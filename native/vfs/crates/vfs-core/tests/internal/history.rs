use super::*;
use crate::fs::vfs::{JournalDelta, MutationTxn, PartialOriginRow};
use crate::fs::{FileSystem, TimeChange, DEFAULT_DIR_MODE, DEFAULT_FILE_MODE};
use crate::{Vfs, VfsOptions};
use rand::{rngs::StdRng, Rng, RngCore, SeedableRng};
use std::collections::HashSet;
use std::fs;
use std::path::PathBuf;
use std::time::Duration;

const TEST_ROOT_INO: i64 = 1;

// Reconstruction intentionally rewrites the journal, root snapshots,
// history markers, and SQLite allocator state, so those tables cannot be
// compared to the source timeline. KV rows, tool calls, and session
// generation are outside filesystem replay by contract. These queries cover
// every replayable live row, ordered by stable primary keys. `fs_chunk` is
// compared separately for every digest reachable from live `fs_data` or
// inline inode bytes; zero-ref rows retained only by journal/snapshot pins
// are excluded because retention and reconstruction legitimately rewrite
// those timelines.
const COMPARED_TABLES: &[(&str, &str, usize)] = &[
    (
        "fs_inode",
        "SELECT ino, mode, nlink, uid, gid, size, atime, mtime, ctime, rdev,
                    atime_nsec, mtime_nsec, ctime_nsec, data_inline, storage_kind
             FROM fs_inode ORDER BY ino",
        15,
    ),
    (
        "fs_dentry",
        "SELECT id, name, parent_ino, ino FROM fs_dentry ORDER BY id",
        4,
    ),
    (
        "fs_data",
        "SELECT ino, chunk_index, digest FROM fs_data ORDER BY ino, chunk_index",
        3,
    ),
    (
        "fs_symlink",
        "SELECT ino, target FROM fs_symlink ORDER BY ino",
        2,
    ),
    (
        "fs_whiteout",
        "SELECT path, parent_path, created_at FROM fs_whiteout ORDER BY path",
        3,
    ),
    (
        "fs_origin",
        "SELECT delta_ino, base_identity FROM fs_origin ORDER BY delta_ino",
        2,
    ),
    (
        "fs_partial_origin",
        "SELECT delta_ino, base_ino, base_path, base_size, base_fingerprint_size,
                    base_mtime, base_mtime_nsec, base_ctime, base_ctime_nsec, created_at
             FROM fs_partial_origin ORDER BY delta_ino",
        10,
    ),
    (
        "fs_chunk_override",
        "SELECT delta_ino, chunk_index FROM fs_chunk_override
             ORDER BY delta_ino, chunk_index",
        2,
    ),
    (
        "fs_overlay_config",
        "SELECT key, value FROM fs_overlay_config ORDER BY key",
        2,
    ),
];

async fn table_dump(conn: &Connection, sql: &str, columns: usize) -> Result<Vec<Vec<String>>> {
    let mut statement_0 = conn.prepare(sql)?;
    let mut rows = statement_0.query(())?;
    let mut dump = Vec::new();
    while let Some(row) = rows.next()? {
        dump.push(
            (0..columns)
                .map(|column| format!("{:?}", row.get::<_, Value>(column)))
                .collect(),
        );
    }
    Ok(dump)
}

async fn live_chunk_dump(conn: &Connection) -> Result<Vec<(Vec<u8>, Vec<u8>, i64)>> {
    let mut live_digests = HashSet::new();
    let mut statement_1 = conn.prepare("SELECT digest FROM fs_data")?;
    let mut rows = statement_1.query(())?;
    while let Some(row) = rows.next()? {
        match row.get::<_, Value>(0)? {
            Value::Blob(digest) => {
                live_digests.insert(digest);
            }
            value => {
                return Err(Error::HistoryIntegrity(format!(
                    "fs_data digest is not a blob in comparison helper: {value:?}"
                )));
            }
        }
    }
    let mut statement_2 =
        conn.prepare("SELECT data_inline FROM fs_inode WHERE data_inline IS NOT NULL")?;
    let mut rows = statement_2.query(())?;
    while let Some(row) = rows.next()? {
        match row.get::<_, Value>(0)? {
            Value::Blob(data) => {
                live_digests.insert(blake3::hash(&data).as_bytes().to_vec());
            }
            value => {
                return Err(Error::HistoryIntegrity(format!(
                    "inline inode data is not a blob in comparison helper: {value:?}"
                )));
            }
        }
    }

    let mut dump = Vec::with_capacity(live_digests.len());
    let mut statement_3 =
        conn.prepare("SELECT digest, data, refcount FROM fs_chunk ORDER BY digest")?;
    let mut rows = statement_3.query(())?;
    while let Some(row) = rows.next()? {
        let Value::Blob(digest) = row.get::<_, Value>(0)? else {
            return Err(Error::HistoryIntegrity(
                "fs_chunk digest is not a blob in comparison helper".to_string(),
            ));
        };
        if !live_digests.remove(&digest) {
            continue;
        }
        let Value::Blob(data) = row.get::<_, Value>(1)? else {
            return Err(Error::HistoryIntegrity(
                "fs_chunk data is not a blob in comparison helper".to_string(),
            ));
        };
        dump.push((digest, data, row.get(2)?));
    }
    if !live_digests.is_empty() {
        return Err(Error::HistoryIntegrity(format!(
            "comparison helper is missing {} live fs_chunk rows",
            live_digests.len()
        )));
    }
    Ok(dump)
}

async fn assert_filesystem_tables_equal(expected: &Path, actual: &Path) -> Result<()> {
    assert_filesystem_tables_equal_for(expected, actual, "deterministic case").await
}

async fn assert_filesystem_tables_equal_for(
    expected: &Path,
    actual: &Path,
    context: &str,
) -> Result<()> {
    let expected_conn = Connection::open(expected)?;
    let actual_conn = Connection::open(actual)?;
    for (table, sql, columns) in COMPARED_TABLES {
        assert_eq!(
            table_dump(&expected_conn, sql, *columns).await?,
            table_dump(&actual_conn, sql, *columns).await?,
            "{context}: table {table} differs"
        );
    }
    assert_eq!(
        live_chunk_dump(&expected_conn).await?,
        live_chunk_dump(&actual_conn).await?,
        "{context}: live-reachable fs_chunk rows differ"
    );
    Ok(())
}

#[tokio::test]
async fn initial_history_floor_contains_the_root_inode() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let source = temp.path().join("initial-source.db");
    let expected = temp.path().join("initial-expected.db");
    let replay = temp.path().join("initial-replay.db");

    let vfs = Vfs::open(VfsOptions::with_path(source.to_string_lossy())).await?;
    let status = vfs.history_status().await?;
    assert_eq!(status.floor_seq, 0);
    vfs.snapshot_into(&expected).await?;
    vfs.fs.finalize().await?;
    drop(vfs);

    fs::copy(&source, &replay)?;
    Vfs::reconstruct_to(&replay, 0).await?;
    assert_filesystem_tables_equal(&expected, &replay).await
}

#[tokio::test]
async fn reconstruct_matches_intermediate_database_exactly() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let source_path = temp.path().join("source.db");
    let expected_path = temp.path().join("expected.db");
    let replay_path = temp.path().join("replay.db");

    let vfs = Vfs::open(VfsOptions::with_path(source_path.to_string_lossy())).await?;
    let (stats, file) =
        FileSystem::create_file(&vfs.fs, 1, "data", DEFAULT_FILE_MODE, 1000, 1000).await?;
    file.pwrite(0, b"inline target").await?;
    FileSystem::link(&vfs.fs, stats.ino, 1, "hardlink").await?;

    let chunk_size = vfs.fs.chunk_size();
    file.pwrite((chunk_size * 2 + 17) as u64, &vec![0x5a; chunk_size + 31])
        .await?;
    let (dense_stats, dense) =
        FileSystem::create_file(&vfs.fs, 1, "dense", DEFAULT_FILE_MODE, 1000, 1000).await?;
    dense.pwrite(0, &vec![0x33; chunk_size]).await?;
    dense.truncate(4).await?;
    let (_, doomed) =
        FileSystem::create_file(&vfs.fs, 1, "doomed", DEFAULT_FILE_MODE, 1000, 1000).await?;
    doomed.pwrite(0, b"reaped").await?;
    drop(doomed);
    FileSystem::unlink(&vfs.fs, 1, "doomed").await?;
    let (_, rename_source) =
        FileSystem::create_file(&vfs.fs, 1, "rename-src", DEFAULT_FILE_MODE, 1000, 1000).await?;
    rename_source.pwrite(0, b"source").await?;
    let (_, rename_destination) =
        FileSystem::create_file(&vfs.fs, 1, "rename-dst", DEFAULT_FILE_MODE, 1000, 1000).await?;
    rename_destination.pwrite(0, b"destination").await?;
    drop(rename_source);
    drop(rename_destination);
    FileSystem::rename(&vfs.fs, 1, "rename-src", 1, "rename-dst").await?;
    vfs.fs.drain_all().await?;
    let conn = Connection::open(&source_path)?;
    assert_eq!(
        query_scalar_i64(
            &conn,
            "SELECT storage_kind FROM fs_inode WHERE ino = ?",
            (dense_stats.ino,),
        )?,
        1,
        "dense chunked file must transition back to inline"
    );
    drop(conn);
    let target = vfs.history_status().await?.head_seq;
    vfs.snapshot_into(&expected_path).await?;

    FileSystem::unlink(&vfs.fs, 1, "hardlink").await?;
    file.truncate(4).await?;
    file.pwrite(0, b"tiny").await?;
    let (_, replacement) =
        FileSystem::create_file(&vfs.fs, 1, "replacement", DEFAULT_FILE_MODE, 1000, 1000).await?;
    replacement.pwrite(0, b"future").await?;
    FileSystem::rename(&vfs.fs, 1, "replacement", 1, "data").await?;
    vfs.fs.finalize().await?;
    drop(vfs);

    fs::copy(&source_path, &replay_path)?;
    Vfs::reconstruct_to(&replay_path, target).await?;
    assert_filesystem_tables_equal(&expected_path, &replay_path).await
}

#[tokio::test]
async fn reconstruct_restores_overlay_sidecars_and_parent_config() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let source_path = temp.path().join("overlay-source.db");
    let expected_path = temp.path().join("overlay-expected.db");
    let replay_path = temp.path().join("overlay-replay.db");

    let vfs = Vfs::open(VfsOptions::with_path(source_path.to_string_lossy())).await?;
    let (stats, file) =
        FileSystem::create_file(&vfs.fs, 1, "partial", DEFAULT_FILE_MODE, 1000, 1000).await?;
    file.pwrite(vfs.fs.chunk_size() as u64, b"partial").await?;
    vfs.fs.drain_all().await?;

    let conn = Connection::open(&source_path)?;
    let mut txn = MutationTxn::begin(&conn, vfs.fs.journal_ctx())?;
    txn.conn().execute(
        "INSERT INTO fs_whiteout (path, parent_path, created_at)
                 VALUES ('/hidden', '/', 7)",
        (),
    )?;
    txn.record(JournalDelta::whiteout_upsert(
        "test_overlay",
        "/hidden",
        "/",
        7,
    ));
    txn.conn().execute(
        "INSERT INTO fs_origin (delta_ino, base_identity) VALUES (?, 'test:42')",
        (stats.ino,),
    )?;
    txn.record(JournalDelta::origin_upsert(
        "test_overlay",
        stats.ino,
        "test:42",
    ));
    let partial = PartialOriginRow {
        delta_ino: stats.ino,
        base_ino: 42,
        base_path: "/partial".to_string(),
        base_size: stats.size.max(vfs.fs.chunk_size() as i64 + 7),
        base_fingerprint_size: 0,
        base_mtime: 1,
        base_mtime_nsec: 2,
        base_ctime: 3,
        base_ctime_nsec: 4,
        created_at: 5,
    };
    txn.conn().execute(
        "INSERT INTO fs_partial_origin
                 (delta_ino, base_ino, base_path, base_size, base_fingerprint_size,
                  base_mtime, base_mtime_nsec, base_ctime, base_ctime_nsec, created_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            partial.delta_ino,
            partial.base_ino,
            partial.base_path.as_str(),
            partial.base_size,
            partial.base_fingerprint_size,
            partial.base_mtime,
            partial.base_mtime_nsec,
            partial.base_ctime,
            partial.base_ctime_nsec,
            partial.created_at,
        ),
    )?;
    txn.record(JournalDelta::partial_origin_upsert(
        "test_overlay",
        &partial,
    ));
    txn.conn().execute(
        "INSERT INTO fs_chunk_override (delta_ino, chunk_index) VALUES (?, 0)",
        (stats.ino,),
    )?;
    txn.record(JournalDelta::chunk_override_upsert(
        "test_overlay",
        stats.ino,
        0,
    ));
    txn.commit()?;
    drop(conn);
    vfs.set_overlay_parent_artifact(&"ab".repeat(32)).await?;

    let target = vfs.history_status().await?.head_seq;
    vfs.snapshot_into(&expected_path).await?;

    let conn = Connection::open(&source_path)?;
    let mut txn = MutationTxn::begin(&conn, vfs.fs.journal_ctx())?;
    txn.conn()
        .execute("DELETE FROM fs_whiteout WHERE path = '/hidden'", ())?;
    txn.record(JournalDelta::whiteout_delete(
        "test_overlay_cleanup",
        "/hidden",
    ));
    txn.conn().execute(
        "DELETE FROM fs_chunk_override WHERE delta_ino = ?",
        (stats.ino,),
    )?;
    txn.record(JournalDelta::chunk_override_delete(
        "test_overlay_cleanup",
        stats.ino,
        0,
    ));
    txn.conn().execute(
        "DELETE FROM fs_partial_origin WHERE delta_ino = ?",
        (stats.ino,),
    )?;
    txn.record(JournalDelta::partial_origin_delete(
        "test_overlay_cleanup",
        stats.ino,
    ));
    txn.conn()
        .execute("DELETE FROM fs_origin WHERE delta_ino = ?", (stats.ino,))?;
    txn.record(JournalDelta::origin_delete(
        "test_overlay_cleanup",
        stats.ino,
    ));
    txn.commit()?;
    drop(conn);
    vfs.clear_overlay_parent_artifact().await?;
    vfs.fs.finalize().await?;
    drop(vfs);

    fs::copy(&source_path, &replay_path)?;
    Vfs::reconstruct_to(&replay_path, target).await?;
    assert_filesystem_tables_equal(&expected_path, &replay_path).await
}

#[tokio::test]
async fn rejects_mid_transaction_and_out_of_range_targets() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let path = temp.path().join("targets.db");
    let vfs = Vfs::open(VfsOptions::with_path(path.to_string_lossy())).await?;
    FileSystem::create_file(&vfs.fs, 1, "target", DEFAULT_FILE_MODE, 1000, 1000).await?;
    let status = vfs.history_status().await?;
    let head = status.head_seq;
    let conn = Connection::open(&path)?;
    let mut statement = conn.prepare("SELECT txn_id FROM fs_op_journal WHERE seq = ?")?;
    let mut rows = statement.query((head,))?;
    let txn_id: i64 = rows.next()?.unwrap().get(0)?;
    if txn_id < head {
        assert!(matches!(
            vfs.validate_target(txn_id).await,
            Err(Error::HistoryTargetMidTransaction { .. })
        ));
    }
    assert!(matches!(
        vfs.validate_target(head + 1).await,
        Err(Error::HistoryTargetOutOfRange { .. })
    ));
    Ok(())
}

#[tokio::test]
async fn capture_root_records_the_drained_head_once() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let path = temp.path().join("capture.db");
    let vfs = Vfs::open(VfsOptions::with_path(path.to_string_lossy())).await?;
    let (_, file) =
        FileSystem::create_file(&vfs.fs, 1, "captured", DEFAULT_FILE_MODE, 1000, 1000).await?;
    file.pwrite(0, b"captured bytes").await?;
    let root = vfs.capture_root("test").await?;
    assert_eq!(root.through_seq, vfs.history_status().await?.head_seq);
    assert_eq!(
        vfs.capture_root("duplicate").await?.snapshot_id,
        root.snapshot_id
    );
    Ok(())
}

#[tokio::test]
async fn kill_switch_invalidates_and_reenabling_starts_new_epoch() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let path = temp.path().join("epoch.db");
    let vfs = Vfs::open(VfsOptions::with_path(path.to_string_lossy())).await?;
    FileSystem::create_file(&vfs.fs, 1, "before-gap", DEFAULT_FILE_MODE, 1000, 1000).await?;
    let old_epoch = vfs.history_status().await?.epoch;
    vfs.fs.finalize().await?;
    drop(vfs);

    let disabled = crate::CoreConfig {
        journal_enabled: false,
        ..crate::CoreConfig::default()
    };
    // A disabled open that mutates nothing is a maintenance open and must
    // not poison history; only the first unjournaled mutation does.
    let vfs =
        Vfs::open(VfsOptions::with_path(path.to_string_lossy()).with_core_config(disabled.clone()))
            .await?;
    assert!(vfs.history_status().await?.valid);
    vfs.fs.finalize().await?;
    drop(vfs);

    let vfs =
        Vfs::open(VfsOptions::with_path(path.to_string_lossy()).with_core_config(disabled)).await?;
    FileSystem::create_file(&vfs.fs, 1, "gap", DEFAULT_FILE_MODE, 1000, 1000).await?;
    let invalid = vfs.history_status().await?;
    assert!(!invalid.valid);
    assert!(matches!(
        vfs.validate_target(invalid.floor_seq).await,
        Err(Error::HistoryInvalid { .. })
    ));
    vfs.fs.finalize().await?;
    drop(vfs);

    let readonly = Vfs::open_read_only(&path).await?;
    assert!(!readonly.history_status().await?.valid);
    drop(readonly);

    let vfs = Vfs::open(VfsOptions::with_path(path.to_string_lossy())).await?;
    let revalidated = vfs.history_status().await?;
    assert!(revalidated.valid);
    assert_eq!(revalidated.epoch, old_epoch + 1);
    assert_eq!(revalidated.floor_seq, revalidated.head_seq);
    assert_eq!(revalidated.targets.len(), 1);
    Ok(())
}

#[tokio::test]
async fn gc_keeps_every_advertised_target_reconstructible() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let path = temp.path().join("gc.db");
    let post_gc = temp.path().join("post-gc.db");
    let config = crate::CoreConfig {
        journal_retention_ops: 8,
        ..crate::CoreConfig::default()
    };
    let vfs =
        Vfs::open(VfsOptions::with_path(path.to_string_lossy()).with_core_config(config)).await?;
    for index in 0..8 {
        let (_, file) = FileSystem::create_file(
            &vfs.fs,
            1,
            &format!("file-{index}"),
            DEFAULT_FILE_MODE,
            1000,
            1000,
        )
        .await?;
        file.pwrite(0, format!("payload-{index}").as_bytes())
            .await?;
    }
    vfs.fs.drain_all().await?;
    let before = vfs.history_status().await?;
    vfs.collect_journal().await?;
    let after = vfs.history_status().await?;
    assert!(after.floor_seq > before.floor_seq);
    assert!(after
        .targets
        .iter()
        .all(|target| target.seq >= after.floor_seq));
    vfs.snapshot_into(&post_gc).await?;
    drop(vfs);

    for target in after.targets {
        let candidate = temp.path().join(format!("gc-target-{}.db", target.seq));
        fs::copy(&post_gc, &candidate)?;
        Vfs::reconstruct_to(&candidate, target.seq).await?;
    }
    Ok(())
}

#[tokio::test]
async fn reconstruction_preserves_inode_high_water_and_exact_refcounts() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let source = temp.path().join("allocator-source.db");
    let replay = temp.path().join("allocator-replay.db");
    let vfs = Vfs::open(VfsOptions::with_path(source.to_string_lossy())).await?;
    FileSystem::create_file(&vfs.fs, 1, "target", DEFAULT_FILE_MODE, 1000, 1000).await?;
    let target = vfs.history_status().await?.head_seq;
    for index in 0..5 {
        FileSystem::create_file(
            &vfs.fs,
            1,
            &format!("future-{index}"),
            DEFAULT_FILE_MODE,
            1000,
            1000,
        )
        .await?;
    }
    let conn = Connection::open(&source)?;
    let max_ever = query_scalar_i64(&conn, "SELECT COALESCE(MAX(ino), 0) FROM fs_inode", ())?;
    drop(conn);
    vfs.fs.finalize().await?;
    drop(vfs);

    fs::copy(&source, &replay)?;
    Vfs::reconstruct_to(&replay, target).await?;
    let vfs = Vfs::open(VfsOptions::with_path(replay.to_string_lossy())).await?;
    let (created, _) =
        FileSystem::create_file(&vfs.fs, 1, "after-replay", DEFAULT_FILE_MODE, 1000, 1000).await?;
    assert!(created.ino > max_ever);
    let conn = Connection::open(&replay)?;
    let mismatches = query_scalar_i64(
        &conn,
        "SELECT COUNT(*) FROM fs_chunk c
             WHERE c.refcount != (
                 SELECT COUNT(*) FROM fs_data d WHERE d.digest = c.digest
             )",
        (),
    )?;
    assert_eq!(mismatches, 0);
    Ok(())
}

#[derive(Clone, Debug)]
struct InventoryEntry {
    parent_ino: i64,
    name: String,
    ino: i64,
    mode: u32,
    nlink: u32,
}

impl InventoryEntry {
    fn is_file(&self) -> bool {
        self.mode & crate::S_IFMT == crate::S_IFREG
    }

    fn is_directory(&self) -> bool {
        self.mode & crate::S_IFMT == crate::S_IFDIR
    }
}

#[derive(Debug)]
struct ReplayCheckpoint {
    seq: i64,
    path: PathBuf,
}

#[derive(Clone, Copy)]
struct RandomReplayCase {
    seed: u64,
    operations: usize,
    gc_each_checkpoint: bool,
    batcher_bursts: bool,
}

async fn load_inventory(vfs: &Vfs) -> Result<Vec<InventoryEntry>> {
    vfs.get_pool()
        .execute(|conn| {
            let mut statement_4 = conn.prepare(
                "SELECT d.parent_ino, d.name, d.ino, i.mode, i.nlink
                 FROM fs_dentry d
                 JOIN fs_inode i ON i.ino = d.ino
                 ORDER BY d.id",
            )?;
            let mut rows = statement_4.query(())?;
            let mut entries = Vec::new();
            while let Some(row) = rows.next()? {
                entries.push(InventoryEntry {
                    parent_ino: row.get(0)?,
                    name: row.get(1)?,
                    ino: row.get(2)?,
                    mode: row.get::<_, i64>(3)? as u32,
                    nlink: row.get::<_, i64>(4)? as u32,
                });
            }
            Ok(entries)
        })
        .await
}

fn choose_entry<'a>(
    entries: &'a [InventoryEntry],
    rng: &mut StdRng,
    predicate: impl Fn(&InventoryEntry) -> bool,
) -> Option<&'a InventoryEntry> {
    let matches = entries
        .iter()
        .filter(|entry| predicate(entry))
        .collect::<Vec<_>>();
    (!matches.is_empty()).then(|| matches[rng.gen_range(0..matches.len())])
}

fn directory_inodes(entries: &[InventoryEntry]) -> Vec<i64> {
    let mut directories = vec![TEST_ROOT_INO];
    directories.extend(
        entries
            .iter()
            .filter(|entry| entry.is_directory())
            .map(|entry| entry.ino),
    );
    directories
}

fn choose_directory(entries: &[InventoryEntry], rng: &mut StdRng) -> i64 {
    let directories = directory_inodes(entries);
    directories[rng.gen_range(0..directories.len())]
}

fn fresh_name(
    entries: &[InventoryEntry],
    parent_ino: i64,
    operation: usize,
    rng: &mut StdRng,
) -> String {
    loop {
        let name = format!("random-{operation}-{:08x}", rng.gen_range(0..=u32::MAX));
        if !entries
            .iter()
            .any(|entry| entry.parent_ino == parent_ino && entry.name == name)
        {
            return name;
        }
    }
}

fn random_bytes(rng: &mut StdRng, len: usize) -> Vec<u8> {
    let mut bytes = vec![0; len];
    rng.fill_bytes(&mut bytes);
    bytes
}

fn random_write(rng: &mut StdRng, chunk_size: usize) -> (u64, Vec<u8>) {
    match rng.gen_range(0..5) {
        0 => {
            let offset = rng.gen_range(0..256);
            let len = rng.gen_range(1..512);
            (offset, random_bytes(rng, len))
        }
        1 => {
            let offset = rng.gen_range(0..1024);
            let len = rng.gen_range(8_000..16_384);
            (offset, random_bytes(rng, len))
        }
        2 => {
            let len = chunk_size + rng.gen_range(1..257);
            ((chunk_size - 31) as u64, random_bytes(rng, len))
        }
        3 => {
            let offset = (chunk_size * 2 + rng.gen_range(1..1024)) as u64;
            let len = rng.gen_range(1..2048);
            (offset, random_bytes(rng, len))
        }
        _ => {
            let offset = rng.gen_range(0..chunk_size) as u64;
            let len = rng.gen_range(1..4096);
            (offset, random_bytes(rng, len))
        }
    }
}

async fn create_random_file(
    vfs: &Vfs,
    entries: &[InventoryEntry],
    operation: usize,
    rng: &mut StdRng,
) -> Result<()> {
    let parent = choose_directory(entries, rng);
    let name = fresh_name(entries, parent, operation, rng);
    let (_, file) =
        FileSystem::create_file(&vfs.fs, parent, &name, DEFAULT_FILE_MODE, 1000, 1000).await?;
    if rng.gen_bool(0.7) {
        let (offset, bytes) = random_write(rng, vfs.fs.chunk_size());
        file.pwrite(offset, &bytes).await?;
    }
    Ok(())
}

async fn run_random_operation(
    vfs: &Vfs,
    operation: usize,
    rng: &mut StdRng,
    protected_ino: Option<i64>,
) -> Result<()> {
    let entries = load_inventory(vfs).await?;
    let roll = rng.gen_range(0..100);
    match roll {
        0..=11 => create_random_file(vfs, &entries, operation, rng).await,
        12..=29 => {
            let Some(entry) = choose_entry(&entries, rng, InventoryEntry::is_file) else {
                return create_random_file(vfs, &entries, operation, rng).await;
            };
            let file = FileSystem::open(&vfs.fs, entry.ino, libc::O_RDWR).await?;
            let (offset, bytes) = random_write(rng, vfs.fs.chunk_size());
            file.pwrite(offset, &bytes).await
        }
        30..=39 => {
            let Some(entry) = choose_entry(&entries, rng, InventoryEntry::is_file) else {
                return create_random_file(vfs, &entries, operation, rng).await;
            };
            let file = FileSystem::open(&vfs.fs, entry.ino, libc::O_RDWR).await?;
            let current_size = file.fstat().await?.size.max(0) as u64;
            let size = match rng.gen_range(0..6) {
                0 => 0,
                1 => current_size / 2,
                2 => 16_383,
                3 => 16_385,
                4 => vfs.fs.chunk_size() as u64 + 37,
                _ => current_size.saturating_add(vfs.fs.chunk_size() as u64 + 19),
            };
            file.truncate(size).await
        }
        40..=47 => {
            let parent = choose_directory(&entries, rng);
            let name = fresh_name(&entries, parent, operation, rng);
            FileSystem::mkdir(&vfs.fs, parent, &name, DEFAULT_DIR_MODE, 1000, 1000)
                .await
                .map(|_| ())
        }
        48..=52 => {
            let occupied = entries
                .iter()
                .map(|entry| entry.parent_ino)
                .collect::<HashSet<_>>();
            let Some(entry) = choose_entry(&entries, rng, |entry| {
                entry.is_directory() && !occupied.contains(&entry.ino)
            }) else {
                return create_random_file(vfs, &entries, operation, rng).await;
            };
            FileSystem::rmdir(&vfs.fs, entry.parent_ino, &entry.name).await
        }
        53..=64 => {
            let Some(source) = choose_entry(&entries, rng, |_| true) else {
                return create_random_file(vfs, &entries, operation, rng).await;
            };
            let replace = rng.gen_bool(0.45);
            if replace {
                let Some(destination) = choose_entry(&entries, rng, |entry| {
                    entry.ino != source.ino
                        && entry.is_directory() == source.is_directory()
                        && !entry.is_directory()
                        && Some(entry.ino) != protected_ino
                }) else {
                    return create_random_file(vfs, &entries, operation, rng).await;
                };
                FileSystem::rename(
                    &vfs.fs,
                    source.parent_ino,
                    &source.name,
                    destination.parent_ino,
                    &destination.name,
                )
                .await
            } else {
                let parent = if source.is_directory() {
                    source.parent_ino
                } else {
                    choose_directory(&entries, rng)
                };
                let name = fresh_name(&entries, parent, operation, rng);
                FileSystem::rename(&vfs.fs, source.parent_ino, &source.name, parent, &name).await
            }
        }
        65..=71 => {
            let Some(entry) = choose_entry(&entries, rng, |entry| {
                !entry.is_directory() && Some(entry.ino) != protected_ino
            }) else {
                return create_random_file(vfs, &entries, operation, rng).await;
            };
            FileSystem::unlink(&vfs.fs, entry.parent_ino, &entry.name).await
        }
        72..=77 => {
            let Some(source) = choose_entry(&entries, rng, InventoryEntry::is_file) else {
                return create_random_file(vfs, &entries, operation, rng).await;
            };
            let parent = choose_directory(&entries, rng);
            let name = fresh_name(&entries, parent, operation, rng);
            FileSystem::link(&vfs.fs, source.ino, parent, &name)
                .await
                .map(|_| ())
        }
        78..=83 => {
            let parent = choose_directory(&entries, rng);
            let name = fresh_name(&entries, parent, operation, rng);
            let target = format!("../target/{operation}/{}", rng.gen::<u64>());
            FileSystem::symlink(&vfs.fs, parent, &name, &target, 1000, 1000)
                .await
                .map(|_| ())
        }
        84..=89 => {
            let ino = if entries.is_empty() {
                TEST_ROOT_INO
            } else {
                entries[rng.gen_range(0..entries.len())].ino
            };
            let mode = [0o600, 0o640, 0o644, 0o700, 0o755][rng.gen_range(0..5)];
            FileSystem::chmod(&vfs.fs, ino, mode).await
        }
        90..=94 => {
            let ino = if entries.is_empty() {
                TEST_ROOT_INO
            } else {
                entries[rng.gen_range(0..entries.len())].ino
            };
            FileSystem::chown(
                &vfs.fs,
                ino,
                Some(rng.gen_range(1_000..2_000)),
                Some(rng.gen_range(1_000..2_000)),
            )
            .await
        }
        95..=97 => {
            let ino = if entries.is_empty() {
                TEST_ROOT_INO
            } else {
                entries[rng.gen_range(0..entries.len())].ino
            };
            let seconds = 1_700_000_000 + operation as i64;
            FileSystem::utimens(
                &vfs.fs,
                ino,
                TimeChange::Set(seconds, rng.gen_range(0..1_000_000_000)),
                TimeChange::Set(seconds + 1, rng.gen_range(0..1_000_000_000)),
            )
            .await
        }
        _ => {
            let Some(entry) = choose_entry(&entries, rng, |entry| {
                entry.is_file() && entry.nlink == 1 && Some(entry.ino) != protected_ino
            }) else {
                return create_random_file(vfs, &entries, operation, rng).await;
            };
            let file = FileSystem::open(&vfs.fs, entry.ino, libc::O_RDWR).await?;
            file.pwrite(0, b"pending-before-unlink").await?;
            FileSystem::unlink(&vfs.fs, entry.parent_ino, &entry.name).await?;
            file.pwrite(9, b"pending-after-unlink").await?;
            drop(file);
            vfs.fs.process_deferred_reaps().await
        }
    }
}

async fn capture_checkpoint(vfs: &Vfs, directory: &Path, label: &str) -> Result<ReplayCheckpoint> {
    let path = directory.join(format!("{label}.db"));
    vfs.snapshot_into(&path).await?;
    Ok(ReplayCheckpoint {
        seq: vfs.history_status().await?.head_seq,
        path,
    })
}

async fn assert_reconstruction(
    source: &Path,
    expected: &Path,
    target: i64,
    candidate: &Path,
    context: &str,
) -> Result<()> {
    fs::copy(source, candidate)?;
    Vfs::reconstruct_to(candidate, target)
        .await
        .map_err(|error| Error::Internal(format!("{context}: {error}")))?;
    assert_filesystem_tables_equal_for(expected, candidate, context).await
}

async fn run_random_replay_case(case: RandomReplayCase) -> Result<()> {
    let temp = tempfile::tempdir()?;
    let source = temp.path().join("source.db");
    let floor_reference = temp.path().join("floor-reference.db");
    let final_reference = temp.path().join("final-reference.db");
    let mut config = crate::CoreConfig::default();
    if case.gc_each_checkpoint {
        config.journal_retention_ops = 1;
    }
    if case.batcher_bursts {
        config.batcher.window = Duration::from_secs(60);
        config.batcher.inode_bytes = usize::MAX / 4;
        config.batcher.global_bytes = usize::MAX / 4;
    }

    let vfs =
        Vfs::open(VfsOptions::with_path(source.to_string_lossy()).with_core_config(config)).await?;
    vfs.snapshot_into(&floor_reference).await?;

    let mut rng = StdRng::seed_from_u64(case.seed);
    let (_, warm_file) = FileSystem::create_file(
        &vfs.fs,
        TEST_ROOT_INO,
        "warm-file",
        DEFAULT_FILE_MODE,
        1000,
        1000,
    )
    .await?;
    warm_file.pwrite(0, b"warm").await?;
    let warm_dir = FileSystem::mkdir(
        &vfs.fs,
        TEST_ROOT_INO,
        "warm-dir",
        DEFAULT_DIR_MODE,
        1000,
        1000,
    )
    .await?;
    FileSystem::symlink(
        &vfs.fs,
        warm_dir.ino,
        "warm-link",
        "../warm-file",
        1000,
        1000,
    )
    .await?;

    let (protected_ino, burst_file) = if case.batcher_bursts {
        let (stats, file) = FileSystem::create_file(
            &vfs.fs,
            TEST_ROOT_INO,
            "batch-target",
            DEFAULT_FILE_MODE,
            1000,
            1000,
        )
        .await?;
        (Some(stats.ino), Some(file))
    } else {
        (None, None)
    };

    let mut checkpoints = vec![capture_checkpoint(&vfs, temp.path(), "checkpoint-000")
        .await
        .map_err(|error| {
            Error::Internal(format!(
                "random replay seed {} initial checkpoint: {error}",
                case.seed
            ))
        })?];

    for operation in 1..=case.operations {
        run_random_operation(&vfs, operation, &mut rng, protected_ino)
            .await
            .map_err(|error| {
                Error::Internal(format!(
                    "random replay seed {} operation {operation}: {error}",
                    case.seed
                ))
            })?;

        if let Some(file) = burst_file.as_ref().filter(|_| operation % 32 == 0) {
            for burst in 0..24 {
                let offset = ((burst * 4093 + operation) % (vfs.fs.chunk_size() * 3)) as u64;
                let bytes = random_bytes(&mut rng, 257 + burst * 19);
                file.pwrite(offset, &bytes).await?;
            }
            file.drain_writes().await?;
        }

        if operation % 40 == 0 {
            let checkpoint =
                capture_checkpoint(&vfs, temp.path(), &format!("checkpoint-{operation:03}"))
                    .await
                    .map_err(|error| {
                        Error::Internal(format!(
                            "random replay seed {} checkpoint {operation}: {error}",
                            case.seed
                        ))
                    })?;
            if case.gc_each_checkpoint {
                let root_mode = if operation / 40 % 2 == 0 {
                    0o755
                } else {
                    0o700
                };
                FileSystem::chmod(&vfs.fs, TEST_ROOT_INO, root_mode).await?;
                vfs.collect_journal().await?;
                assert_eq!(
                    vfs.history_status().await?.floor_seq,
                    checkpoint.seq,
                    "random replay seed {}: GC floor did not land on checkpoint {}",
                    case.seed,
                    operation
                );
            }
            checkpoints.push(checkpoint);
        }
    }

    vfs.snapshot_into(&final_reference).await?;
    let final_status = vfs.history_status().await?;
    vfs.fs.finalize().await?;
    drop(burst_file);
    drop(warm_file);
    drop(vfs);

    for (index, checkpoint) in checkpoints.iter().enumerate() {
        let candidate = temp.path().join(format!("replay-checkpoint-{index:03}.db"));
        fs::copy(&source, &candidate)?;
        let context = format!(
            "random replay seed {} checkpoint {} target {}",
            case.seed, index, checkpoint.seq
        );
        if checkpoint.seq < final_status.floor_seq {
            assert!(
                matches!(
                    Vfs::reconstruct_to(&candidate, checkpoint.seq).await,
                    Err(Error::HistoryTargetOutOfRange {
                        target_seq,
                        floor_seq,
                        head_seq,
                        epoch,
                    }) if target_seq == checkpoint.seq
                        && floor_seq == final_status.floor_seq
                        && head_seq == final_status.head_seq
                        && epoch == final_status.epoch
                ),
                "{context}: checkpoint below the retained floor was not refused"
            );
        } else {
            Vfs::reconstruct_to(&candidate, checkpoint.seq)
                .await
                .map_err(|error| Error::Internal(format!("{context}: {error}")))?;
            assert_filesystem_tables_equal_for(&checkpoint.path, &candidate, &context).await?;
        }
    }

    let floor_expected = if final_status.floor_seq == 0 {
        floor_reference.as_path()
    } else {
        checkpoints
            .iter()
            .find(|checkpoint| checkpoint.seq == final_status.floor_seq)
            .map(|checkpoint| checkpoint.path.as_path())
            .ok_or_else(|| {
                Error::Internal(format!(
                    "random replay seed {} has no exact floor checkpoint for {}",
                    case.seed, final_status.floor_seq
                ))
            })?
    };
    assert_reconstruction(
        &source,
        floor_expected,
        final_status.floor_seq,
        &temp.path().join("replay-floor.db"),
        &format!(
            "random replay seed {} explicit floor {}",
            case.seed, final_status.floor_seq
        ),
    )
    .await?;
    assert_reconstruction(
        &source,
        &final_reference,
        final_status.head_seq,
        &temp.path().join("replay-head.db"),
        &format!(
            "random replay seed {} explicit head {}",
            case.seed, final_status.head_seq
        ),
    )
    .await
}

#[tokio::test]
async fn randomized_replay_conformance_seed_0x5eed() -> Result<()> {
    run_random_replay_case(RandomReplayCase {
        seed: 0x5eed,
        operations: 320,
        gc_each_checkpoint: false,
        batcher_bursts: false,
    })
    .await
}

#[tokio::test]
async fn randomized_replay_conformance_seed_0xc0ffee() -> Result<()> {
    run_random_replay_case(RandomReplayCase {
        seed: 0xc0ffee,
        operations: 320,
        gc_each_checkpoint: false,
        batcher_bursts: false,
    })
    .await
}

#[tokio::test]
async fn randomized_replay_conformance_seed_0xdecafbad() -> Result<()> {
    run_random_replay_case(RandomReplayCase {
        seed: 0xdecafbad,
        operations: 320,
        gc_each_checkpoint: false,
        batcher_bursts: false,
    })
    .await
}

#[tokio::test]
async fn randomized_replay_conformance_with_gc_roll_forward() -> Result<()> {
    run_random_replay_case(RandomReplayCase {
        seed: 0x6c5eed,
        operations: 320,
        gc_each_checkpoint: true,
        batcher_bursts: false,
    })
    .await
}

#[tokio::test]
async fn randomized_replay_conformance_with_batcher_bursts() -> Result<()> {
    run_random_replay_case(RandomReplayCase {
        seed: 0x0ba7_c4e2,
        operations: 320,
        gc_each_checkpoint: false,
        batcher_bursts: true,
    })
    .await
}
