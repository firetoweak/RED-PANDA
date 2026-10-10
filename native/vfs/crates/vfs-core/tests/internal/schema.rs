use super::*;
use crate::{Vfs, VfsOptions, DEFAULT_FILE_MODE};
use tempfile::tempdir;

const S_IFDIR: i64 = 0o040000;
const S_IFREG: i64 = 0o100000;

#[tokio::test]
async fn empty_chunk_with_nonempty_digest_is_corruption() -> Result<()> {
    let dir = tempdir()?;
    let db_path = dir.path().join("empty-chunk-corruption.db");
    let conn = Connection::open(db_path.to_str().unwrap())?;
    ensure_current(&conn)?;
    conn.execute(
        "INSERT INTO fs_chunk (digest, data, refcount) VALUES (?, ?, 0)",
        (
            Value::Blob(blake3::hash(b"missing bytes").as_bytes().to_vec()),
            Value::Blob(Vec::new()),
        ),
    )?;

    let report = integrity::check(&conn, &integrity::CheckOpts::new(db_path.clone()))?;
    assert!(!report.ok);
    assert!(report.checks.iter().any(|check| {
        check.name == "storage.chunk_bytes_match_digest"
            && !check.ok
            && check.detail.contains("1 violation")
    }));
    Ok(())
}

#[tokio::test]
async fn open_paths_reject_old_schema_without_upgrading() -> Result<()> {
    for version in [
        SchemaVersion::V0_0,
        SchemaVersion::V0_2,
        SchemaVersion::V0_4,
        SchemaVersion::V0_5,
        SchemaVersion::V0_6,
        SchemaVersion::V0_7,
        SchemaVersion::V0_8,
        SchemaVersion::V0_9,
        SchemaVersion::V0_10,
        SchemaVersion::V0_11,
    ] {
        let dir = tempdir()?;
        let db_path = dir.path().join(format!("old-{}.db", version.as_str()));
        let marker = if version >= SchemaVersion::V0_8 {
            version.user_version()
        } else {
            0
        };
        {
            let conn = Connection::open(db_path.to_str().unwrap())?;
            create_legacy_fixture(&conn, version)?;
            conn.execute(&format!("PRAGMA user_version = {marker}"), ())?;
        }

        let err = match Vfs::open(VfsOptions::with_path(db_path.to_string_lossy())).await {
            Ok(_) => panic!("{version}: Vfs::open must not upgrade an old schema"),
            Err(err) => err,
        };
        assert!(
            matches!(err, Error::SchemaVersionMismatch { .. }),
            "{version}: unexpected open error {err}"
        );
        let conn = Connection::open(db_path.to_str().unwrap())?;
        assert_eq!(user_version(&conn)?, marker, "{version}: marker changed");
        assert_eq!(detect_schema_version(&conn)?, Some(version));
        let columns = get_table_columns(&conn, "fs_inode")?;
        if version < SchemaVersion::V0_5 {
            assert!(
                !columns.iter().any(|column| column.name == "data_inline"),
                "{version}: open added v0.5 columns"
            );
        }

        let before = read_fixture_file_bytes(&conn)?;
        assert!(matches!(
            ensure_current(&conn),
            Err(Error::SchemaVersionMismatch { .. })
        ));
        assert_eq!(user_version(&conn)?, marker);
        assert_eq!(before, read_fixture_file_bytes(&conn)?);
    }
    Ok(())
}

#[tokio::test]
async fn schema_interrupted_init_reopens_or_errors_cleanly() -> Result<()> {
    let dir = tempdir()?;
    let empty_path = dir.path().join("empty.db");
    let conn = Connection::open(empty_path.to_str().unwrap())?;
    ensure_current(&conn)?;
    assert_eq!(user_version(&conn)?, CURRENT.user_version());

    let config_only_path = dir.path().join("config-only.db");
    let conn = Connection::open(config_only_path.to_str().unwrap())?;
    conn.execute(
        "CREATE TABLE fs_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        (),
    )?;
    ensure_current(&conn)?;
    assert_eq!(user_version(&conn)?, CURRENT.user_version());

    let hybrid_path = dir.path().join("hybrid-v05-no-markers.db");
    let conn = Connection::open(hybrid_path.to_str().unwrap())?;
    create_legacy_fixture(&conn, SchemaVersion::V0_5)?;
    conn.execute(
        "DELETE FROM fs_config WHERE key = ?",
        (CONFIG_SCHEMA_VERSION_KEY,),
    )?;
    assert!(matches!(
        ensure_current(&conn),
        Err(Error::SchemaVersionMismatch { .. })
    ));
    assert_eq!(detect_schema_version(&conn)?, Some(SchemaVersion::V0_5));

    let corrupt_current_path = dir.path().join("current-missing-table.db");
    let conn = Connection::open(corrupt_current_path.to_str().unwrap())?;
    set_user_version(&conn, CURRENT)?;
    let err = ensure_current(&conn).expect_err("missing current tables must error");
    assert!(
        err.to_string()
            .contains("current schema is missing required table"),
        "unexpected error: {err}"
    );

    Ok(())
}

fn create_legacy_fixture(conn: &Connection, version: SchemaVersion) -> Result<()> {
    conn.execute(
        "CREATE TABLE fs_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        (),
    )?;
    conn.execute(
        "INSERT INTO fs_config (key, value) VALUES ('chunk_size', '4')",
        (),
    )?;
    conn.execute(
            "INSERT INTO fs_config (key, value) VALUES ('schema_version', ?), ('inline_threshold', '4')",
            (version.as_str(),),
        )?;

    let mut columns = vec![
        "ino INTEGER PRIMARY KEY AUTOINCREMENT",
        "mode INTEGER NOT NULL",
        "uid INTEGER NOT NULL DEFAULT 0",
        "gid INTEGER NOT NULL DEFAULT 0",
        "size INTEGER NOT NULL DEFAULT 0",
        "atime INTEGER NOT NULL",
        "mtime INTEGER NOT NULL",
        "ctime INTEGER NOT NULL",
    ];
    if version >= SchemaVersion::V0_2 {
        columns.insert(2, "nlink INTEGER NOT NULL DEFAULT 0");
    }
    if version >= SchemaVersion::V0_4 {
        columns.extend([
            "rdev INTEGER NOT NULL DEFAULT 0",
            "atime_nsec INTEGER NOT NULL DEFAULT 0",
            "mtime_nsec INTEGER NOT NULL DEFAULT 0",
            "ctime_nsec INTEGER NOT NULL DEFAULT 0",
        ]);
    }
    if version >= SchemaVersion::V0_5 {
        columns.extend([
            "data_inline BLOB",
            "storage_kind INTEGER NOT NULL DEFAULT 0",
        ]);
    }
    conn.execute(
        &format!("CREATE TABLE fs_inode ({})", columns.join(", ")),
        (),
    )?;
    conn.execute(
        "CREATE TABLE fs_dentry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                parent_ino INTEGER NOT NULL,
                ino INTEGER NOT NULL,
                UNIQUE(parent_ino, name)
            )",
        (),
    )?;
    if version >= SchemaVersion::V0_7 {
        conn.execute(
            "CREATE TABLE fs_data (
                    ino INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    digest BLOB NOT NULL,
                    PRIMARY KEY (ino, chunk_index)
                )",
            (),
        )?;
        conn.execute(
            "CREATE TABLE fs_chunk (
                    digest BLOB PRIMARY KEY,
                    data BLOB NOT NULL,
                    refcount INTEGER NOT NULL DEFAULT 0
                )",
            (),
        )?;
        conn.execute(
            "CREATE TABLE fs_op_journal (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    txn_id INTEGER NOT NULL,
                    op TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    wallclock_ms INTEGER NOT NULL
                )",
            (),
        )?;
        conn.execute(
            "CREATE TABLE fs_journal_chunk (
                    seq INTEGER NOT NULL,
                    digest BLOB NOT NULL
                )",
            (),
        )?;
    } else {
        conn.execute(
            "CREATE TABLE fs_data (
                    ino INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    data BLOB NOT NULL,
                    PRIMARY KEY (ino, chunk_index)
                )",
            (),
        )?;
    }
    conn.execute(
        "CREATE TABLE fs_symlink (ino INTEGER PRIMARY KEY, target TEXT NOT NULL)",
        (),
    )?;
    conn.execute(
        "CREATE TABLE kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                created_at INTEGER DEFAULT (unixepoch()),
                updated_at INTEGER DEFAULT (unixepoch())
            )",
        (),
    )?;
    conn.execute(
        "CREATE TABLE tool_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                parameters TEXT,
                result TEXT,
                error TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                started_at INTEGER NOT NULL,
                completed_at INTEGER,
                duration_ms INTEGER
            )",
        (),
    )?;
    if version >= SchemaVersion::V0_6 {
        conn.execute(
            "CREATE TABLE fs_session_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )",
            (),
        )?;
    }

    insert_legacy_inode(conn, version, 1, S_IFDIR | 0o755, 2, 0)?;
    insert_legacy_inode(conn, version, 2, S_IFREG | DEFAULT_FILE_MODE as i64, 1, 6)?;
    insert_legacy_inode(conn, version, 3, S_IFREG | DEFAULT_FILE_MODE as i64, 1, 4)?;
    conn.execute(
        "INSERT INTO fs_dentry (name, parent_ino, ino) VALUES
             ('file.txt', 1, 2),
             ('duplicate.txt', 1, 3)",
        (),
    )?;
    if version >= SchemaVersion::V0_7 {
        let abcd = blake3::hash(b"abcd").as_bytes().to_vec();
        let ef = blake3::hash(b"ef").as_bytes().to_vec();
        conn.execute(
            "INSERT INTO fs_chunk (digest, data, refcount) VALUES
                 (?, ?, 2),
                 (?, ?, 1)",
            (
                Value::Blob(abcd.clone()),
                Value::Blob(b"abcd".to_vec()),
                Value::Blob(ef.clone()),
                Value::Blob(b"ef".to_vec()),
            ),
        )?;
        conn.execute(
            "INSERT INTO fs_data (ino, chunk_index, digest) VALUES
                 (2, 0, ?),
                 (2, 1, ?),
                 (3, 0, ?)",
            (
                Value::Blob(abcd.clone()),
                Value::Blob(ef),
                Value::Blob(abcd.clone()),
            ),
        )?;
        conn.execute(
            "INSERT INTO fs_op_journal (txn_id, op, payload, wallclock_ms)
                 VALUES (1, 'write', '{\"ino\":2,\"ranges\":[]}', 1)",
            (),
        )?;
        conn.execute(
            "INSERT INTO fs_journal_chunk (seq, digest) VALUES (1, ?)",
            (Value::Blob(abcd),),
        )?;
    } else {
        conn.execute(
            "INSERT INTO fs_data (ino, chunk_index, data) VALUES
                 (2, 0, ?),
                 (2, 1, ?),
                 (3, 0, ?)",
            (
                Value::Blob(b"abcd".to_vec()),
                Value::Blob(b"ef".to_vec()),
                Value::Blob(b"abcd".to_vec()),
            ),
        )?;
    }
    conn.execute(
        "INSERT INTO kv_store (key, value) VALUES ('k', '{\"v\":1}')",
        (),
    )?;
    conn.execute(
            "INSERT INTO tool_calls (name, parameters, status, started_at) VALUES ('tool', '{}', 'success', 1)",
            (),
        )?;
    Ok(())
}

fn insert_legacy_inode(
    conn: &Connection,
    version: SchemaVersion,
    ino: i64,
    mode: i64,
    nlink: i64,
    size: i64,
) -> Result<()> {
    let mut columns = vec![
        "ino", "mode", "uid", "gid", "size", "atime", "mtime", "ctime",
    ];
    let mut values = vec![
        Value::Integer(ino),
        Value::Integer(mode),
        Value::Integer(0),
        Value::Integer(0),
        Value::Integer(size),
        Value::Integer(1),
        Value::Integer(1),
        Value::Integer(1),
    ];
    if version >= SchemaVersion::V0_2 {
        columns.insert(2, "nlink");
        values.insert(2, Value::Integer(nlink));
    }
    if version >= SchemaVersion::V0_4 {
        columns.extend(["rdev", "atime_nsec", "mtime_nsec", "ctime_nsec"]);
        values.extend([
            Value::Integer(0),
            Value::Integer(0),
            Value::Integer(0),
            Value::Integer(0),
        ]);
    }
    if version >= SchemaVersion::V0_5 {
        columns.extend(["data_inline", "storage_kind"]);
        values.extend([Value::Null, Value::Integer(0)]);
    }
    let placeholders = std::iter::repeat_n("?", columns.len())
        .collect::<Vec<_>>()
        .join(", ");
    conn.execute(
        &format!(
            "INSERT INTO fs_inode ({}) VALUES ({})",
            columns.join(", "),
            placeholders
        ),
        tokio_rusqlite::rusqlite::params_from_iter(values),
    )?;
    Ok(())
}

fn read_fixture_file_bytes(conn: &Connection) -> Result<Vec<u8>> {
    let sql = if get_table_columns(conn, "fs_data")?
        .iter()
        .any(|c| c.name == "data")
    {
        "SELECT data FROM fs_data WHERE ino = 2 ORDER BY chunk_index"
    } else {
        "SELECT c.data
             FROM fs_data d
             JOIN fs_chunk c ON c.digest = d.digest
             WHERE d.ino = 2
             ORDER BY d.chunk_index"
    };
    let mut statement_2 = conn.prepare(sql)?;
    let mut rows = statement_2.query(())?;
    let mut bytes = Vec::new();
    while let Some(row) = rows.next()? {
        match row.get::<_, Value>(0)? {
            Value::Blob(chunk) => bytes.extend(chunk),
            other => {
                return Err(Error::Internal(format!(
                    "unexpected fs_data value in fixture: {other:?}"
                )))
            }
        }
    }
    Ok(bytes)
}
