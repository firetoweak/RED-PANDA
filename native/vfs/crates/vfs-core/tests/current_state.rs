use vfs_core::error::Result;
use vfs_core::{FileSystem, Vfs, VfsOptions};

#[tokio::test]
async fn mutations_keep_only_current_state_and_sqlite_recovery() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let sdk = Vfs::open(VfsOptions::with_path(
        dir.path().join("state.db").to_string_lossy(),
    ))
    .await?;
    let (stats, file) = FileSystem::create_file(&sdk.fs, 1, "file", 0o644, 0, 0).await?;
    file.pwrite(0, b"current bytes").await?;
    file.truncate(7).await?;
    file.fsync().await?;
    sdk.fs.rename(1, "file", 1, "renamed").await?;
    assert_eq!(file.pread(0, 64).await?, b"current");
    assert_eq!(sdk.fs.lookup(1, "renamed").await?.unwrap().ino, stats.ino);
    sdk.fs
        .get_pool()
        .execute(|conn| {
            let history_tables: i64 = conn.query_row(
                "SELECT COUNT(*) FROM sqlite_schema WHERE type = 'table'
             AND (name LIKE 'fs_snapshot%' OR name LIKE 'fs_journal%'
                  OR name IN ('fs_op_journal', 'fs_session_metadata'))",
                [],
                |row| row.get(0),
            )?;
            assert_eq!(history_tables, 0);
            let history_markers: i64 = conn.query_row(
                "SELECT COUNT(*) FROM fs_config WHERE key LIKE 'history_%'",
                [],
                |row| row.get(0),
            )?;
            assert_eq!(history_markers, 0);
            let mode: String = conn.query_row("PRAGMA journal_mode", [], |row| row.get(0))?;
            assert_eq!(mode, "wal");
            Ok(())
        })
        .await?;
    Ok(())
}

#[tokio::test]
async fn chunk_collection_preserves_live_sharing_and_frozen_artifacts() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let sdk = Vfs::open(VfsOptions::with_path(
        dir.path().join("live.db").to_string_lossy(),
    ))
    .await?;
    let chunk_size = sdk.fs.chunk_size();
    let mut original = vec![1; chunk_size];
    original.extend(vec![2; chunk_size]);
    let (_, first) = FileSystem::create_file(&sdk.fs, 1, "first", 0o644, 0, 0).await?;
    let (_, second) = FileSystem::create_file(&sdk.fs, 1, "second", 0o644, 0, 0).await?;
    first.pwrite(0, &original).await?;
    second.pwrite(0, &original).await?;
    // Both buffered writes must be included without requiring explicit fsync.
    let frozen_path = dir.path().join("frozen.db");
    sdk.snapshot_into(&frozen_path).await?;
    let frozen_bytes = std::fs::read(&frozen_path)?;
    let frozen = Vfs::open_read_only(&frozen_path).await?;
    first.pwrite(0, &vec![3; chunk_size]).await?;
    // The other file still references the old first chunk, so it cannot be collected.
    assert_eq!(sdk.collect_unused_chunks().await?, 0);
    second.truncate(0).await?;
    assert_eq!(sdk.collect_unused_chunks().await?, 1);
    let mut changed = vec![3; chunk_size];
    changed.extend(vec![2; chunk_size]);
    assert_eq!(first.pread(0, changed.len() as u64).await?, changed);
    for path in ["/first", "/second"] {
        assert_eq!(
            frozen
                .fs
                .open(path)
                .await?
                .pread(0, original.len() as u64)
                .await?,
            original
        );
    }
    first.truncate(0).await?;
    assert_eq!(sdk.collect_unused_chunks().await?, 2);
    assert_eq!(sdk.collect_unused_chunks().await?, 0);
    frozen.fs.finalize().await?;
    assert_eq!(std::fs::read(&frozen_path)?, frozen_bytes);
    Ok(())
}
