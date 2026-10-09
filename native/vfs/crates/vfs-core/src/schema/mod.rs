//! Schema authority for Vfs databases.
//!
//! This module owns all production Rust DDL, schema-version detection, and
//! current-format initialization and validation.

pub mod integrity;

use crate::config::{DEFAULT_CHUNK_SIZE, DEFAULT_INLINE_THRESHOLD};
use crate::error::{Error, Result};
use std::time::{SystemTime, UNIX_EPOCH};
use turso::transaction::{Transaction, TransactionBehavior};
use turso::{Connection, Value};

/// Current schema version.
pub const CURRENT: SchemaVersion = SchemaVersion::V0_11;

/// Only the current local filesystem format is accepted.
pub const MIN_SUPPORTED: SchemaVersion = SchemaVersion::V0_11;

/// Current persisted format marker.
pub const VFS_SCHEMA_VERSION: &str = CURRENT.as_str();
pub const CONFIG_SCHEMA_VERSION_KEY: &str = "schema_version";
pub const CONFIG_CHUNK_SIZE_KEY: &str = "chunk_size";
pub const CONFIG_INLINE_THRESHOLD_KEY: &str = "inline_threshold";
pub const CONFIG_HISTORY_EPOCH_KEY: &str = "history_epoch";
pub const CONFIG_HISTORY_VALID_KEY: &str = "history_valid";
pub const CONFIG_HISTORY_FLOOR_SEQ_KEY: &str = "history_floor_seq";
pub(crate) const CONFIG_FILESYSTEM_ID_KEY: &str = "filesystem_id";

/// Detected schema version. Legacy markers are recognized only to reject old
/// formats with an explicit version; they do not enable compatibility.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum SchemaVersion {
    /// Base schema: fs_inode, fs_dentry, fs_data, fs_symlink, fs_config, kv_store, tool_calls
    V0_0,
    /// Added nlink column to fs_inode
    V0_2,
    /// Added atime_nsec, mtime_nsec, ctime_nsec, rdev columns to fs_inode
    V0_4,
    /// Added inline small-file storage columns and overlay sidecar tables
    V0_5,
    /// Added persistent session handoff metadata
    V0_6,
    /// Added content-addressed chunk storage and operation-journal schema
    V0_7,
    /// Added replayable row-delta history and relational root snapshots
    V0_8,
    /// Persistent base identities replace process-local origin inode numbers.
    V0_9,
    /// Persistent filesystem namespaces distinguish files born in different deltas.
    V0_10,
    /// Filesystem-only local storage; KV, tool tracking and remote chunks removed.
    V0_11,
}

impl std::fmt::Display for SchemaVersion {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

impl SchemaVersion {
    /// Returns the version string.
    pub const fn as_str(self) -> &'static str {
        match self {
            SchemaVersion::V0_0 => "0.0",
            SchemaVersion::V0_2 => "0.2",
            SchemaVersion::V0_4 => "0.4",
            SchemaVersion::V0_5 => "0.5",
            SchemaVersion::V0_6 => "0.6",
            SchemaVersion::V0_7 => "0.7",
            SchemaVersion::V0_8 => "0.8",
            SchemaVersion::V0_9 => "0.9",
            SchemaVersion::V0_10 => "0.10",
            SchemaVersion::V0_11 => "0.11",
        }
    }

    /// Returns the PRAGMA user_version value for this schema.
    pub const fn user_version(self) -> i64 {
        match self {
            SchemaVersion::V0_0 => 0,
            SchemaVersion::V0_2 => 2,
            SchemaVersion::V0_4 => 4,
            SchemaVersion::V0_5 => 5,
            SchemaVersion::V0_6 => 6,
            SchemaVersion::V0_7 => 7,
            SchemaVersion::V0_8 => 8,
            SchemaVersion::V0_9 => 9,
            SchemaVersion::V0_10 => 10,
            SchemaVersion::V0_11 => 11,
        }
    }

    /// Returns true if this version is the current version.
    pub const fn is_current(self) -> bool {
        matches!(self, CURRENT)
    }

    /// Parse a version marker string (e.g. "0.4") into a known schema version.
    pub fn parse(marker: &str) -> Option<Self> {
        match marker {
            "0.0" => Some(SchemaVersion::V0_0),
            "0.2" => Some(SchemaVersion::V0_2),
            "0.4" => Some(SchemaVersion::V0_4),
            "0.5" => Some(SchemaVersion::V0_5),
            "0.6" => Some(SchemaVersion::V0_6),
            "0.7" => Some(SchemaVersion::V0_7),
            "0.8" => Some(SchemaVersion::V0_8),
            "0.9" => Some(SchemaVersion::V0_9),
            "0.10" => Some(SchemaVersion::V0_10),
            "0.11" => Some(SchemaVersion::V0_11),
            _ => None,
        }
    }

    fn from_user_version(version: i64) -> Option<Self> {
        match version {
            0 => Some(SchemaVersion::V0_0),
            2 => Some(SchemaVersion::V0_2),
            4 => Some(SchemaVersion::V0_4),
            5 => Some(SchemaVersion::V0_5),
            6 => Some(SchemaVersion::V0_6),
            7 => Some(SchemaVersion::V0_7),
            8 => Some(SchemaVersion::V0_8),
            9 => Some(SchemaVersion::V0_9),
            10 => Some(SchemaVersion::V0_10),
            11 => Some(SchemaVersion::V0_11),
            _ => None,
        }
    }
}

/// Single production DDL source.
mod ddl {
    use super::SchemaVersion;

    /// Returns all DDL statements needed for the requested schema version.
    pub(crate) fn create_all(_version: SchemaVersion) -> &'static [&'static str] {
        CURRENT_DDL
    }

    const CURRENT_DDL: &[&str] = &[
        "CREATE TABLE IF NOT EXISTS fs_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )",
        "CREATE TABLE IF NOT EXISTS fs_inode (
            ino INTEGER PRIMARY KEY AUTOINCREMENT,
            mode INTEGER NOT NULL,
            nlink INTEGER NOT NULL DEFAULT 0,
            uid INTEGER NOT NULL DEFAULT 0,
            gid INTEGER NOT NULL DEFAULT 0,
            size INTEGER NOT NULL DEFAULT 0,
            atime INTEGER NOT NULL,
            mtime INTEGER NOT NULL,
            ctime INTEGER NOT NULL,
            rdev INTEGER NOT NULL DEFAULT 0,
            atime_nsec INTEGER NOT NULL DEFAULT 0,
            mtime_nsec INTEGER NOT NULL DEFAULT 0,
            ctime_nsec INTEGER NOT NULL DEFAULT 0,
            data_inline BLOB,
            storage_kind INTEGER NOT NULL DEFAULT 0
        )",
        "CREATE TABLE IF NOT EXISTS fs_dentry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            parent_ino INTEGER NOT NULL,
            ino INTEGER NOT NULL,
            UNIQUE(parent_ino, name)
        )",
        "CREATE INDEX IF NOT EXISTS idx_fs_dentry_parent ON fs_dentry(parent_ino, name)",
        "CREATE INDEX IF NOT EXISTS idx_fs_dentry_parent_ino ON fs_dentry(parent_ino, ino)",
        "CREATE TABLE IF NOT EXISTS fs_data (
            ino INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            digest BLOB NOT NULL,
            PRIMARY KEY (ino, chunk_index)
        )",
        "CREATE TABLE IF NOT EXISTS fs_chunk (
            digest BLOB PRIMARY KEY,
            data BLOB NOT NULL,
            refcount INTEGER NOT NULL DEFAULT 0
        )",
        "CREATE TABLE IF NOT EXISTS fs_symlink (
            ino INTEGER PRIMARY KEY,
            target TEXT NOT NULL
        )",
        "CREATE TABLE IF NOT EXISTS fs_whiteout (
            path TEXT PRIMARY KEY,
            parent_path TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )",
        "CREATE INDEX IF NOT EXISTS idx_fs_whiteout_parent ON fs_whiteout(parent_path)",
        "CREATE TABLE IF NOT EXISTS fs_overlay_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )",
        "CREATE TABLE IF NOT EXISTS fs_session_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )",
        // The op journal deliberately carries no secondary index and no pin
        // table: every mutating commit would pay their B-tree writes, and
        // both are derivable — txn_id equals the group's first seq (groups
        // are contiguous in the primary key), and the digests history
        // references are parsed from retained rows by offline collection.
        JOURNAL_V2_DDL,
        "CREATE TABLE IF NOT EXISTS fs_snapshot (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            through_seq INTEGER NOT NULL,
            created_at_ms INTEGER NOT NULL,
            reason TEXT NOT NULL,
            history_epoch INTEGER NOT NULL,
            UNIQUE(history_epoch, through_seq)
        )",
        "CREATE TABLE IF NOT EXISTS fs_snapshot_inode (
            snapshot_id INTEGER NOT NULL,
            ino INTEGER NOT NULL,
            mode INTEGER NOT NULL,
            nlink INTEGER NOT NULL,
            uid INTEGER NOT NULL,
            gid INTEGER NOT NULL,
            size INTEGER NOT NULL,
            atime INTEGER NOT NULL,
            mtime INTEGER NOT NULL,
            ctime INTEGER NOT NULL,
            rdev INTEGER NOT NULL,
            atime_nsec INTEGER NOT NULL,
            mtime_nsec INTEGER NOT NULL,
            ctime_nsec INTEGER NOT NULL,
            data_inline_digest BLOB,
            storage_kind INTEGER NOT NULL,
            PRIMARY KEY (snapshot_id, ino)
        )",
        "CREATE TABLE IF NOT EXISTS fs_snapshot_dentry (
            snapshot_id INTEGER NOT NULL,
            id INTEGER NOT NULL,
            name TEXT NOT NULL,
            parent_ino INTEGER NOT NULL,
            ino INTEGER NOT NULL,
            PRIMARY KEY (snapshot_id, id),
            UNIQUE(snapshot_id, parent_ino, name)
        )",
        "CREATE TABLE IF NOT EXISTS fs_snapshot_data (
            snapshot_id INTEGER NOT NULL,
            ino INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            digest BLOB NOT NULL,
            PRIMARY KEY (snapshot_id, ino, chunk_index)
        )",
        "CREATE TABLE IF NOT EXISTS fs_snapshot_symlink (
            snapshot_id INTEGER NOT NULL,
            ino INTEGER NOT NULL,
            target TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, ino)
        )",
        "CREATE TABLE IF NOT EXISTS fs_snapshot_whiteout (
            snapshot_id INTEGER NOT NULL,
            path TEXT NOT NULL,
            parent_path TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (snapshot_id, path)
        )",
        "CREATE TABLE IF NOT EXISTS fs_snapshot_origin (
            snapshot_id INTEGER NOT NULL,
            delta_ino INTEGER NOT NULL,
            base_identity TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, delta_ino)
        )",
        "CREATE TABLE IF NOT EXISTS fs_snapshot_partial_origin (
            snapshot_id INTEGER NOT NULL,
            delta_ino INTEGER NOT NULL,
            base_ino INTEGER NOT NULL,
            base_path TEXT NOT NULL,
            base_size INTEGER NOT NULL,
            base_fingerprint_size INTEGER NOT NULL,
            base_mtime INTEGER NOT NULL,
            base_mtime_nsec INTEGER NOT NULL,
            base_ctime INTEGER NOT NULL,
            base_ctime_nsec INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (snapshot_id, delta_ino)
        )",
        "CREATE TABLE IF NOT EXISTS fs_snapshot_chunk_override (
            snapshot_id INTEGER NOT NULL,
            delta_ino INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            PRIMARY KEY (snapshot_id, delta_ino, chunk_index)
        )",
        "CREATE TABLE IF NOT EXISTS fs_snapshot_chunk (
            snapshot_id INTEGER NOT NULL,
            digest BLOB NOT NULL,
            PRIMARY KEY (snapshot_id, digest)
        )",
        "CREATE INDEX IF NOT EXISTS idx_fs_snapshot_chunk_digest ON fs_snapshot_chunk(digest)",
        "CREATE TABLE IF NOT EXISTS fs_snapshot_meta (
            snapshot_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, key)
        )",
        "CREATE TABLE IF NOT EXISTS fs_origin (
            delta_ino INTEGER PRIMARY KEY,
            base_identity TEXT NOT NULL UNIQUE
        )",
        "CREATE TABLE IF NOT EXISTS fs_partial_origin (
            delta_ino INTEGER PRIMARY KEY,
            base_ino INTEGER NOT NULL,
            base_path TEXT NOT NULL,
            base_size INTEGER NOT NULL,
            base_fingerprint_size INTEGER NOT NULL DEFAULT -1,
            base_mtime INTEGER NOT NULL DEFAULT 0,
            base_mtime_nsec INTEGER NOT NULL DEFAULT 0,
            base_ctime INTEGER NOT NULL DEFAULT 0,
            base_ctime_nsec INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )",
        "CREATE TABLE IF NOT EXISTS fs_chunk_override (
            delta_ino INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            PRIMARY KEY (delta_ino, chunk_index)
        )",
    ];

    pub(crate) const JOURNAL_V2_DDL: &str = "CREATE TABLE IF NOT EXISTS fs_op_journal (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        txn_id INTEGER NOT NULL,
        label TEXT NOT NULL,
        tbl TEXT NOT NULL,
        verb TEXT NOT NULL,
        row TEXT NOT NULL,
        wallclock_ms INTEGER NOT NULL
    )";
}

#[derive(Debug)]
struct ColumnInfo {
    name: String,
    type_name: String,
    not_null: bool,
    default_value: Option<String>,
}

#[derive(Clone, Copy)]
struct ColumnSpec {
    table_name: &'static str,
    column_name: &'static str,
    type_name: &'static str,
    not_null: bool,
    default_value: Option<&'static str>,
}

const CURRENT_COLUMN_SPECS: &[ColumnSpec] = &[
    ColumnSpec {
        table_name: "fs_origin",
        column_name: "base_identity",
        type_name: "TEXT",
        not_null: true,
        default_value: None,
    },
    ColumnSpec {
        table_name: "fs_snapshot_origin",
        column_name: "base_identity",
        type_name: "TEXT",
        not_null: true,
        default_value: None,
    },
    ColumnSpec {
        table_name: "fs_inode",
        column_name: "nlink",
        type_name: "INTEGER",
        not_null: true,
        default_value: Some("0"),
    },
    ColumnSpec {
        table_name: "fs_inode",
        column_name: "atime_nsec",
        type_name: "INTEGER",
        not_null: true,
        default_value: Some("0"),
    },
    ColumnSpec {
        table_name: "fs_inode",
        column_name: "mtime_nsec",
        type_name: "INTEGER",
        not_null: true,
        default_value: Some("0"),
    },
    ColumnSpec {
        table_name: "fs_inode",
        column_name: "ctime_nsec",
        type_name: "INTEGER",
        not_null: true,
        default_value: Some("0"),
    },
    ColumnSpec {
        table_name: "fs_inode",
        column_name: "rdev",
        type_name: "INTEGER",
        not_null: true,
        default_value: Some("0"),
    },
    ColumnSpec {
        table_name: "fs_inode",
        column_name: "data_inline",
        type_name: "BLOB",
        not_null: false,
        default_value: None,
    },
    ColumnSpec {
        table_name: "fs_inode",
        column_name: "storage_kind",
        type_name: "INTEGER",
        not_null: true,
        default_value: Some("0"),
    },
    ColumnSpec {
        table_name: "fs_data",
        column_name: "digest",
        type_name: "BLOB",
        not_null: true,
        default_value: None,
    },
    ColumnSpec {
        table_name: "fs_op_journal",
        column_name: "label",
        type_name: "TEXT",
        not_null: true,
        default_value: None,
    },
    ColumnSpec {
        table_name: "fs_op_journal",
        column_name: "tbl",
        type_name: "TEXT",
        not_null: true,
        default_value: None,
    },
    ColumnSpec {
        table_name: "fs_op_journal",
        column_name: "verb",
        type_name: "TEXT",
        not_null: true,
        default_value: None,
    },
    ColumnSpec {
        table_name: "fs_op_journal",
        column_name: "row",
        type_name: "TEXT",
        not_null: true,
        default_value: None,
    },
];

const REQUIRED_CURRENT_TABLES: &[&str] = &[
    "fs_config",
    "fs_inode",
    "fs_dentry",
    "fs_data",
    "fs_chunk",
    "fs_symlink",
    "fs_whiteout",
    "fs_overlay_config",
    "fs_session_metadata",
    "fs_op_journal",
    "fs_snapshot",
    "fs_snapshot_inode",
    "fs_snapshot_dentry",
    "fs_snapshot_data",
    "fs_snapshot_symlink",
    "fs_snapshot_whiteout",
    "fs_snapshot_origin",
    "fs_snapshot_partial_origin",
    "fs_snapshot_chunk_override",
    "fs_snapshot_chunk",
    "fs_snapshot_meta",
    "fs_origin",
    "fs_partial_origin",
    "fs_chunk_override",
];

/// Detect the schema version of an existing database.
///
/// Returns `None` if the database has no `fs_inode` table and is therefore a
/// new database from the schema authority's perspective.
pub async fn detect_schema_version(conn: &Connection) -> Result<Option<SchemaVersion>> {
    let raw_user_version = user_version(conn).await?;
    if raw_user_version > 0 {
        let version = SchemaVersion::from_user_version(raw_user_version).ok_or_else(|| {
            Error::SchemaVersionMismatch {
                found: format!("user_version {raw_user_version}"),
                expected: CURRENT.to_string(),
            }
        })?;
        return Ok(Some(version));
    }

    if !table_exists(conn, "fs_inode").await? {
        return Ok(None);
    }

    if table_exists(conn, "fs_snapshot").await? {
        return Ok(Some(SchemaVersion::V0_8));
    }

    if table_exists(conn, "fs_chunk").await? {
        return Ok(Some(SchemaVersion::V0_7));
    }

    let columns = get_table_columns(conn, "fs_inode").await?;
    let has_nlink = columns.iter().any(|c| c.name == "nlink");
    let has_atime_nsec = columns.iter().any(|c| c.name == "atime_nsec");
    let has_mtime_nsec = columns.iter().any(|c| c.name == "mtime_nsec");
    let has_ctime_nsec = columns.iter().any(|c| c.name == "ctime_nsec");
    let has_rdev = columns.iter().any(|c| c.name == "rdev");
    let has_data_inline = columns.iter().any(|c| c.name == "data_inline");
    let has_storage_kind = columns.iter().any(|c| c.name == "storage_kind");

    if has_data_inline && has_storage_kind && table_exists(conn, "fs_session_metadata").await? {
        return Ok(Some(SchemaVersion::V0_6));
    }

    // Pre-user_version v0.5 databases are recognized by columns. The old
    // fs_config markers are compatibility hints, not authoritative identity.
    if has_data_inline && has_storage_kind {
        return Ok(Some(SchemaVersion::V0_5));
    }

    if has_atime_nsec && has_mtime_nsec && has_ctime_nsec && has_rdev {
        return Ok(Some(SchemaVersion::V0_4));
    }

    if has_nlink {
        return Ok(Some(SchemaVersion::V0_2));
    }

    Ok(Some(SchemaVersion::V0_0))
}

/// Check that a database has the current schema version.
///
/// This is a read-only check. Opening paths should call [`ensure_current`] so
/// a fresh database is initialized and current tables are validated.
pub async fn check_schema_version(conn: &Connection) -> Result<()> {
    if let Some(version) = detect_schema_version(conn).await? {
        if !version.is_current() {
            return Err(Error::SchemaVersionMismatch {
                found: version.to_string(),
                expected: CURRENT.to_string(),
            });
        }
        validate_current_schema(conn).await?;
    }
    Ok(())
}

/// Initialize a new database or validate the current format. Older formats
/// are rejected before any DDL or data changes.
pub async fn ensure_current(conn: &Connection) -> Result<()> {
    let raw_user_version = user_version(conn).await?;
    let detected = detect_schema_version(conn).await?;

    if let Some(version) = detected {
        if version < MIN_SUPPORTED {
            return Err(Error::SchemaVersionMismatch {
                found: version.to_string(),
                expected: CURRENT.to_string(),
            });
        }
    }
    if raw_user_version == CURRENT.user_version() {
        validate_current_schema(conn).await?;
        ensure_current_indexes(conn).await?;
        return Ok(());
    }
    if detected == Some(CURRENT) {
        filesystem_identity(conn).await?;
    }

    let txn = Transaction::new_unchecked(conn, TransactionBehavior::Immediate).await?;
    let result = async {
        execute_current_ddl(conn).await?;
        if detected.is_none() {
            conn.execute(
                "INSERT INTO fs_config (key, value) VALUES (?, ?)",
                (CONFIG_FILESYSTEM_ID_KEY, uuid::Uuid::new_v4().to_string()),
            )
            .await?;
        }
        ensure_config_defaults(conn).await?;
        if detected.is_none() {
            capture_root_raw(conn, "init", 1, 0)
                .await
                .map_err(|error| {
                    Error::Internal(format!(
                        "failed to capture initial fs_inode history root: {error}"
                    ))
                })?;
        }
        set_user_version(conn, CURRENT).await?;
        Ok(())
    }
    .await;

    match result {
        Ok(()) => txn.commit().await?,
        Err(err) => {
            let _ = txn.rollback().await;
            return Err(err);
        }
    }

    validate_current_schema(conn).await?;
    Ok(())
}

/// Set or update the overlay base-path marker without owning any DDL locally.
pub(crate) async fn set_overlay_base_path(conn: &Connection, base_path: &str) -> Result<()> {
    ensure_current(conn).await?;
    conn.execute(
        "INSERT OR REPLACE INTO fs_overlay_config (key, value) VALUES ('base_path', ?1)",
        [Value::Text(base_path.to_string())],
    )
    .await?;
    Ok(())
}

async fn execute_current_ddl(conn: &Connection) -> Result<()> {
    for sql in ddl::create_all(CURRENT) {
        conn.execute(*sql, ()).await?;
    }
    Ok(())
}

async fn ensure_current_indexes(conn: &Connection) -> Result<()> {
    // Schema validation checks columns; ensure planner indexes also exist.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fs_dentry_parent ON fs_dentry(parent_ino, name)",
        (),
    )
    .await?;
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fs_dentry_parent_ino ON fs_dentry(parent_ino, ino)",
        (),
    )
    .await?;
    Ok(())
}

async fn ensure_config_defaults(conn: &Connection) -> Result<()> {
    conn.execute(
        "INSERT OR REPLACE INTO fs_config (key, value) VALUES (?, ?)",
        (CONFIG_SCHEMA_VERSION_KEY, CURRENT.as_str()),
    )
    .await?;
    conn.execute(
        "INSERT OR IGNORE INTO fs_config (key, value) VALUES (?, ?)",
        (CONFIG_CHUNK_SIZE_KEY, DEFAULT_CHUNK_SIZE.to_string()),
    )
    .await?;
    // A defaulted inline threshold must fit the recorded chunk size.
    let chunk_size = read_config_value(conn, CONFIG_CHUNK_SIZE_KEY)
        .await?
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(DEFAULT_CHUNK_SIZE);
    conn.execute(
        "INSERT OR IGNORE INTO fs_config (key, value) VALUES (?, ?)",
        (
            CONFIG_INLINE_THRESHOLD_KEY,
            DEFAULT_INLINE_THRESHOLD.min(chunk_size).to_string(),
        ),
    )
    .await?;
    initialize_history_markers(conn).await?;
    Ok(())
}

async fn initialize_history_markers(conn: &Connection) -> Result<()> {
    for (key, value) in [
        (CONFIG_HISTORY_EPOCH_KEY, "1"),
        (CONFIG_HISTORY_VALID_KEY, "1"),
        (CONFIG_HISTORY_FLOOR_SEQ_KEY, "0"),
    ] {
        conn.execute(
            "INSERT OR IGNORE INTO fs_config (key, value) VALUES (?, ?)",
            (key, value),
        )
        .await?;
    }
    Ok(())
}

/// Replace the schema-created empty init root after Vfs installs inode 1.
///
/// Raw schema creation leaves live filesystem rows empty. A normal writable
/// Vfs open then creates inode 1. Only that pristine state may rewrite the
/// sequence-0 root; repaired/corrupt databases with any journal or populated
/// snapshot state keep their existing lineage.
pub(crate) async fn refresh_empty_initial_root(conn: &Connection) -> Result<()> {
    let mut rows = conn
        .query(
            "SELECT
                 (SELECT COUNT(*) FROM fs_op_journal),
                 (SELECT COUNT(*) FROM fs_snapshot),
                 (SELECT COUNT(*) FROM fs_snapshot_inode),
                 (SELECT COUNT(*) FROM fs_snapshot
                  WHERE history_epoch = 1 AND through_seq = 0 AND reason = 'init')",
            (),
        )
        .await?;
    let row = rows
        .next()
        .await?
        .ok_or_else(|| Error::Internal("failed to inspect the initial history root".to_string()))?;
    let journal_rows: i64 = row.get(0)?;
    let snapshot_rows: i64 = row.get(1)?;
    let snapshot_inode_rows: i64 = row.get(2)?;
    let matching_init_rows: i64 = row.get(3)?;
    drop(rows);
    if (
        journal_rows,
        snapshot_rows,
        snapshot_inode_rows,
        matching_init_rows,
    ) != (0, 1, 0, 1)
    {
        return Ok(());
    }

    let mut rows = conn
        .query(
            "SELECT snapshot_id FROM fs_snapshot
             WHERE history_epoch = 1 AND through_seq = 0 AND reason = 'init'",
            (),
        )
        .await?;
    let snapshot_id: i64 = rows
        .next()
        .await?
        .ok_or_else(|| Error::Internal("initial history root disappeared".to_string()))?
        .get(0)?;
    drop(rows);

    for table in [
        "fs_snapshot_inode",
        "fs_snapshot_dentry",
        "fs_snapshot_data",
        "fs_snapshot_symlink",
        "fs_snapshot_whiteout",
        "fs_snapshot_origin",
        "fs_snapshot_partial_origin",
        "fs_snapshot_chunk_override",
        "fs_snapshot_chunk",
        "fs_snapshot_meta",
    ] {
        conn.execute(
            &format!("DELETE FROM {table} WHERE snapshot_id = ?"),
            (snapshot_id,),
        )
        .await?;
    }
    conn.execute(
        "DELETE FROM fs_snapshot WHERE snapshot_id = ?",
        (snapshot_id,),
    )
    .await?;
    capture_root_raw(conn, "init", 1, 0).await?;
    Ok(())
}

/// Capture the live filesystem and overlay root inside the caller's
/// transaction. This helper neither drains pending writes nor acquires a
/// session lock; callers own the consistency boundary.
pub async fn capture_root_raw(
    conn: &Connection,
    reason: &str,
    epoch: i64,
    through_seq: i64,
) -> Result<i64> {
    let created_at_ms = i64::try_from(SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis())
        .map_err(|_| Error::Internal("snapshot timestamp overflow".to_string()))?;
    conn.execute(
        "INSERT INTO fs_snapshot
         (through_seq, created_at_ms, reason, history_epoch)
         VALUES (?, ?, ?, ?)",
        (through_seq, created_at_ms, reason, epoch),
    )
    .await?;
    let snapshot_id = conn.last_insert_rowid();

    conn.execute(
        "INSERT INTO fs_snapshot_inode (
            snapshot_id, ino, mode, nlink, uid, gid, size, atime, mtime, ctime,
            rdev, atime_nsec, mtime_nsec, ctime_nsec, data_inline_digest, storage_kind
         )
         SELECT ?, ino, mode, nlink, uid, gid, size, atime, mtime, ctime,
                rdev, atime_nsec, mtime_nsec, ctime_nsec, NULL, storage_kind
         FROM fs_inode",
        (snapshot_id,),
    )
    .await?;
    conn.execute(
        "INSERT INTO fs_snapshot_dentry
         (snapshot_id, id, name, parent_ino, ino)
         SELECT ?, id, name, parent_ino, ino FROM fs_dentry",
        (snapshot_id,),
    )
    .await?;
    conn.execute(
        "INSERT INTO fs_snapshot_data
         (snapshot_id, ino, chunk_index, digest)
         SELECT ?, ino, chunk_index, digest FROM fs_data",
        (snapshot_id,),
    )
    .await?;
    conn.execute(
        "INSERT INTO fs_snapshot_symlink (snapshot_id, ino, target)
         SELECT ?, ino, target FROM fs_symlink",
        (snapshot_id,),
    )
    .await?;
    conn.execute(
        "INSERT INTO fs_snapshot_whiteout
         (snapshot_id, path, parent_path, created_at)
         SELECT ?, path, parent_path, created_at FROM fs_whiteout",
        (snapshot_id,),
    )
    .await?;
    conn.execute(
        "INSERT INTO fs_snapshot_origin (snapshot_id, delta_ino, base_identity)
         SELECT ?, delta_ino, base_identity FROM fs_origin",
        (snapshot_id,),
    )
    .await?;
    conn.execute(
        "INSERT INTO fs_snapshot_partial_origin (
            snapshot_id, delta_ino, base_ino, base_path, base_size,
            base_fingerprint_size, base_mtime, base_mtime_nsec,
            base_ctime, base_ctime_nsec, created_at
         )
         SELECT ?, delta_ino, base_ino, base_path, base_size,
                base_fingerprint_size, base_mtime, base_mtime_nsec,
                base_ctime, base_ctime_nsec, created_at
         FROM fs_partial_origin",
        (snapshot_id,),
    )
    .await?;
    conn.execute(
        "INSERT INTO fs_snapshot_chunk_override
         (snapshot_id, delta_ino, chunk_index)
         SELECT ?, delta_ino, chunk_index FROM fs_chunk_override",
        (snapshot_id,),
    )
    .await?;
    conn.execute(
        "INSERT INTO fs_snapshot_chunk (snapshot_id, digest)
         SELECT ?, digest FROM fs_data GROUP BY digest",
        (snapshot_id,),
    )
    .await?;

    let mut rows = conn
        .query(
            "SELECT ino, data_inline
             FROM fs_inode
             WHERE data_inline IS NOT NULL
             ORDER BY ino",
            (),
        )
        .await?;
    let mut inline_rows = Vec::new();
    while let Some(row) = rows.next().await? {
        inline_rows.push((row.get::<i64>(0)?, row.get::<Vec<u8>>(1)?));
    }
    drop(rows);
    for (ino, data) in inline_rows {
        let digest = blake3::hash(&data).as_bytes().to_vec();
        conn.execute(
            "INSERT INTO fs_chunk (digest, data, refcount)
             VALUES (?, ?, 0)
             ON CONFLICT(digest) DO NOTHING",
            (Value::Blob(digest.clone()), Value::Blob(data)),
        )
        .await?;
        conn.execute(
            "UPDATE fs_snapshot_inode
             SET data_inline_digest = ?
             WHERE snapshot_id = ? AND ino = ?",
            (Value::Blob(digest.clone()), snapshot_id, ino),
        )
        .await?;
        conn.execute(
            "INSERT OR IGNORE INTO fs_snapshot_chunk (snapshot_id, digest)
             VALUES (?, ?)",
            (snapshot_id, Value::Blob(digest)),
        )
        .await?;
    }

    for (meta_key, table, source_key) in [
        ("seed_pin", "fs_session_metadata", "seed_pin"),
        ("seeded_paths", "fs_session_metadata", "seeded_paths"),
        ("parent_artifact", "fs_overlay_config", "parent_artifact"),
    ] {
        let sql = format!(
            "INSERT INTO fs_snapshot_meta (snapshot_id, key, value)
             SELECT ?, ?, value FROM {table} WHERE key = ?"
        );
        conn.execute(&sql, (snapshot_id, meta_key, source_key))
            .await?;
    }

    Ok(snapshot_id)
}

/// Rebuild the retained row-delta journal so its AUTOINCREMENT allocator
/// resumes immediately after `through_seq`.
///
/// Historical reconstruction trims a future suffix. Turso rejects direct
/// writes to `sqlite_sequence`, so rebuilding the table is the only supported
/// way to prevent the next committed group from leaving a false gap.
pub async fn rebuild_journal_allocator(conn: &Connection, through_seq: i64) -> Result<()> {
    conn.execute(
        "CREATE TABLE fs_op_journal_rebuilt (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            txn_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            tbl TEXT NOT NULL,
            verb TEXT NOT NULL,
            row TEXT NOT NULL,
            wallclock_ms INTEGER NOT NULL
        )",
        (),
    )
    .await?;
    conn.execute(
        "INSERT INTO fs_op_journal_rebuilt
         (seq, txn_id, label, tbl, verb, row, wallclock_ms)
         SELECT seq, txn_id, label, tbl, verb, row, wallclock_ms
         FROM fs_op_journal
         WHERE seq <= ?
         ORDER BY seq",
        (through_seq,),
    )
    .await?;
    conn.execute("DROP TABLE fs_op_journal", ()).await?;
    conn.execute(
        "ALTER TABLE fs_op_journal_rebuilt RENAME TO fs_op_journal",
        (),
    )
    .await?;
    Ok(())
}

async fn read_config_value(conn: &Connection, key: &str) -> Result<Option<String>> {
    let mut rows = conn
        .query("SELECT value FROM fs_config WHERE key = ?", (key,))
        .await?;
    if let Some(row) = rows.next().await? {
        Ok(Some(row.get(0)?))
    } else {
        Ok(None)
    }
}

async fn validate_current_schema(conn: &Connection) -> Result<()> {
    for table in REQUIRED_CURRENT_TABLES {
        if !table_exists(conn, table).await? {
            return Err(Error::Internal(format!(
                "current schema is missing required table {table}"
            )));
        }
    }

    for spec in CURRENT_COLUMN_SPECS {
        ensure_column_matches(conn, *spec).await?;
    }

    filesystem_identity(conn).await?;

    Ok(())
}

/// Immutable creation namespace; never default missing or corrupt persisted identity.
pub(crate) async fn filesystem_identity(conn: &Connection) -> Result<String> {
    let value = read_config_value(conn, CONFIG_FILESYSTEM_ID_KEY)
        .await?
        .ok_or_else(|| Error::Internal("current schema is missing filesystem_id".into()))?;
    if !valid_filesystem_identity(&value) {
        return Err(Error::Internal(format!(
            "invalid filesystem_id {value:?}: expected canonical UUID v4"
        )));
    }
    Ok(value)
}

pub(crate) fn valid_filesystem_identity(value: &str) -> bool {
    uuid::Uuid::parse_str(value)
        .is_ok_and(|id| id.get_version_num() == 4 && id.to_string() == value)
}

async fn user_version(conn: &Connection) -> Result<i64> {
    let mut rows = conn.query("PRAGMA user_version", ()).await?;
    let row = rows
        .next()
        .await?
        .ok_or_else(|| Error::Internal("PRAGMA user_version returned no rows".to_string()))?;
    row.get(0).map_err(Error::from)
}

async fn set_user_version(conn: &Connection, version: SchemaVersion) -> Result<()> {
    conn.execute(
        &format!("PRAGMA user_version = {}", version.user_version()),
        (),
    )
    .await?;
    Ok(())
}

async fn table_exists(conn: &Connection, table_name: &str) -> Result<bool> {
    let mut rows = conn
        .query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        .await?;
    Ok(rows.next().await?.is_some())
}

async fn get_table_columns(conn: &Connection, table_name: &str) -> Result<Vec<ColumnInfo>> {
    let mut rows = conn
        .query(&format!("PRAGMA table_info({})", table_name), ())
        .await?;

    let mut columns = Vec::new();
    while let Some(row) = rows.next().await? {
        let name: String = row.get(1)?;
        let type_name: String = row.get(2)?;
        let not_null: i64 = row.get(3)?;
        let default_value = match row.get_value(4).ok() {
            Some(Value::Text(value)) => Some(value.clone()),
            Some(Value::Integer(value)) => Some(value.to_string()),
            Some(Value::Null) | None => None,
            Some(value) => Some(format!("{value:?}")),
        };
        columns.push(ColumnInfo {
            name,
            type_name,
            not_null: not_null != 0,
            default_value,
        });
    }

    Ok(columns)
}

async fn ensure_column_matches(conn: &Connection, spec: ColumnSpec) -> Result<()> {
    let columns = get_table_columns(conn, spec.table_name).await?;
    for column in columns {
        if column.name != spec.column_name {
            continue;
        }

        let type_matches = column.type_name.eq_ignore_ascii_case(spec.type_name);
        let default_matches = column.default_value.as_deref() == spec.default_value;
        if type_matches && column.not_null == spec.not_null && default_matches {
            return Ok(());
        }

        return Err(Error::Internal(format!(
            "schema column {}.{} already exists with incompatible definition: \
             expected type={} not_null={} default={:?}; \
             found type={} not_null={} default={:?}",
            spec.table_name,
            spec.column_name,
            spec.type_name,
            spec.not_null,
            spec.default_value,
            column.type_name,
            column.not_null,
            column.default_value
        )));
    }

    Err(Error::Internal(format!(
        "schema column {}.{} is missing",
        spec.table_name, spec.column_name
    )))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Vfs, VfsOptions, DEFAULT_FILE_MODE};
    use tempfile::tempdir;
    use turso::Builder;

    const S_IFDIR: i64 = 0o040000;
    const S_IFREG: i64 = 0o100000;

    #[tokio::test]
    async fn empty_chunk_with_nonempty_digest_is_corruption() -> Result<()> {
        let dir = tempdir()?;
        let db_path = dir.path().join("empty-chunk-corruption.db");
        let db = Builder::new_local(db_path.to_str().unwrap())
            .build()
            .await?;
        let conn = db.connect()?;
        ensure_current(&conn).await?;
        conn.execute(
            "INSERT INTO fs_chunk (digest, data, refcount) VALUES (?, ?, 0)",
            (
                Value::Blob(blake3::hash(b"missing bytes").as_bytes().to_vec()),
                Value::Blob(Vec::new()),
            ),
        )
        .await?;

        let report = integrity::check(&conn, &integrity::CheckOpts::new(db_path.clone())).await?;
        assert!(!report.ok);
        assert!(report.checks.iter().any(|check| {
            check.name == "storage.chunk_bytes_match_digest"
                && !check.ok
                && check.detail.contains("1 violation")
        }));
        Ok(())
    }

    #[tokio::test]
    async fn capture_root_normalizes_and_pins_inline_bytes() -> Result<()> {
        let dir = tempdir()?;
        let db_path = dir.path().join("capture-root-inline.db");
        let db = Builder::new_local(db_path.to_str().unwrap())
            .build()
            .await?;
        let conn = db.connect()?;
        ensure_current(&conn).await?;

        let inline = b"snapshot-inline".to_vec();
        let digest = blake3::hash(&inline).as_bytes().to_vec();
        conn.execute(
            "INSERT INTO fs_inode (
                ino, mode, nlink, uid, gid, size, atime, mtime, ctime, rdev,
                atime_nsec, mtime_nsec, ctime_nsec, data_inline, storage_kind
             ) VALUES (2, ?, 1, 1, 2, ?, 1, 1, 1, 0, 0, 0, 0, ?, 1)",
            (
                S_IFREG | DEFAULT_FILE_MODE as i64,
                inline.len() as i64,
                Value::Blob(inline.clone()),
            ),
        )
        .await?;
        conn.execute(
            "INSERT INTO fs_dentry (name, parent_ino, ino) VALUES ('inline.txt', 1, 2)",
            (),
        )
        .await?;

        let snapshot_id = capture_root_raw(&conn, "test", 1, 1).await?;
        let mut rows = conn
            .query(
                "SELECT data_inline_digest
                 FROM fs_snapshot_inode
                 WHERE snapshot_id = ? AND ino = 2",
                (snapshot_id,),
            )
            .await?;
        let row = rows.next().await?.expect("snapshot inode must exist");
        assert_eq!(row.get::<Vec<u8>>(0)?, digest);
        drop(rows);

        let mut rows = conn
            .query(
                "SELECT c.data, c.refcount
                 FROM fs_snapshot_chunk sc
                 JOIN fs_chunk c ON c.digest = sc.digest
                 WHERE sc.snapshot_id = ? AND sc.digest = ?",
                (snapshot_id, Value::Blob(digest)),
            )
            .await?;
        let row = rows
            .next()
            .await?
            .expect("inline snapshot digest must be pinned");
        assert_eq!(row.get::<Vec<u8>>(0)?, inline);
        assert_eq!(row.get::<i64>(1)?, 0);
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
        ] {
            let dir = tempdir()?;
            let db_path = dir.path().join(format!("old-{}.db", version.as_str()));
            let marker = if version >= SchemaVersion::V0_8 {
                version.user_version()
            } else {
                0
            };
            {
                let db = Builder::new_local(db_path.to_str().unwrap())
                    .build()
                    .await?;
                let conn = db.connect()?;
                create_legacy_fixture(&conn, version).await?;
                conn.execute(&format!("PRAGMA user_version = {marker}"), ())
                    .await?;
            }

            let err = match Vfs::open(VfsOptions::with_path(db_path.to_string_lossy())).await {
                Ok(_) => panic!("{version}: Vfs::open must not upgrade an old schema"),
                Err(err) => err,
            };
            assert!(
                matches!(err, Error::SchemaVersionMismatch { .. }),
                "{version}: unexpected open error {err}"
            );
            let db = Builder::new_local(db_path.to_str().unwrap())
                .build()
                .await?;
            let conn = db.connect()?;
            assert_eq!(
                user_version(&conn).await?,
                marker,
                "{version}: marker changed"
            );
            assert_eq!(detect_schema_version(&conn).await?, Some(version));
            let columns = get_table_columns(&conn, "fs_inode").await?;
            if version < SchemaVersion::V0_5 {
                assert!(
                    !columns.iter().any(|column| column.name == "data_inline"),
                    "{version}: open added v0.5 columns"
                );
            }

            let before = read_fixture_file_bytes(&conn).await?;
            assert!(matches!(
                ensure_current(&conn).await,
                Err(Error::SchemaVersionMismatch { .. })
            ));
            assert_eq!(user_version(&conn).await?, marker);
            assert_eq!(before, read_fixture_file_bytes(&conn).await?);
        }
        Ok(())
    }

    #[tokio::test]
    async fn schema_interrupted_init_reopens_or_errors_cleanly() -> Result<()> {
        let dir = tempdir()?;
        let empty_path = dir.path().join("empty.db");
        let db = Builder::new_local(empty_path.to_str().unwrap())
            .build()
            .await?;
        let conn = db.connect()?;
        ensure_current(&conn).await?;
        assert_eq!(user_version(&conn).await?, CURRENT.user_version());

        let config_only_path = dir.path().join("config-only.db");
        let db = Builder::new_local(config_only_path.to_str().unwrap())
            .build()
            .await?;
        let conn = db.connect()?;
        conn.execute(
            "CREATE TABLE fs_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            (),
        )
        .await?;
        ensure_current(&conn).await?;
        assert_eq!(user_version(&conn).await?, CURRENT.user_version());

        let hybrid_path = dir.path().join("hybrid-v05-no-markers.db");
        let db = Builder::new_local(hybrid_path.to_str().unwrap())
            .build()
            .await?;
        let conn = db.connect()?;
        create_legacy_fixture(&conn, SchemaVersion::V0_5).await?;
        conn.execute(
            "DELETE FROM fs_config WHERE key = ?",
            (CONFIG_SCHEMA_VERSION_KEY,),
        )
        .await?;
        assert!(matches!(
            ensure_current(&conn).await,
            Err(Error::SchemaVersionMismatch { .. })
        ));
        assert_eq!(
            detect_schema_version(&conn).await?,
            Some(SchemaVersion::V0_5)
        );

        let corrupt_current_path = dir.path().join("current-missing-table.db");
        let db = Builder::new_local(corrupt_current_path.to_str().unwrap())
            .build()
            .await?;
        let conn = db.connect()?;
        set_user_version(&conn, CURRENT).await?;
        let err = ensure_current(&conn)
            .await
            .expect_err("missing current tables must error");
        assert!(
            err.to_string()
                .contains("current schema is missing required table"),
            "unexpected error: {err}"
        );

        Ok(())
    }

    async fn create_legacy_fixture(conn: &Connection, version: SchemaVersion) -> Result<()> {
        conn.execute(
            "CREATE TABLE fs_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            (),
        )
        .await?;
        conn.execute(
            "INSERT INTO fs_config (key, value) VALUES ('chunk_size', '4')",
            (),
        )
        .await?;
        conn.execute(
            "INSERT INTO fs_config (key, value) VALUES ('schema_version', ?), ('inline_threshold', '4')",
            (version.as_str(),),
        )
        .await?;

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
        )
        .await?;
        conn.execute(
            "CREATE TABLE fs_dentry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                parent_ino INTEGER NOT NULL,
                ino INTEGER NOT NULL,
                UNIQUE(parent_ino, name)
            )",
            (),
        )
        .await?;
        if version >= SchemaVersion::V0_7 {
            conn.execute(
                "CREATE TABLE fs_data (
                    ino INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    digest BLOB NOT NULL,
                    PRIMARY KEY (ino, chunk_index)
                )",
                (),
            )
            .await?;
            conn.execute(
                "CREATE TABLE fs_chunk (
                    digest BLOB PRIMARY KEY,
                    data BLOB NOT NULL,
                    refcount INTEGER NOT NULL DEFAULT 0
                )",
                (),
            )
            .await?;
            conn.execute(
                "CREATE TABLE fs_op_journal (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    txn_id INTEGER NOT NULL,
                    op TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    wallclock_ms INTEGER NOT NULL
                )",
                (),
            )
            .await?;
            conn.execute(
                "CREATE TABLE fs_journal_chunk (
                    seq INTEGER NOT NULL,
                    digest BLOB NOT NULL
                )",
                (),
            )
            .await?;
        } else {
            conn.execute(
                "CREATE TABLE fs_data (
                    ino INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    data BLOB NOT NULL,
                    PRIMARY KEY (ino, chunk_index)
                )",
                (),
            )
            .await?;
        }
        conn.execute(
            "CREATE TABLE fs_symlink (ino INTEGER PRIMARY KEY, target TEXT NOT NULL)",
            (),
        )
        .await?;
        conn.execute(
            "CREATE TABLE kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                created_at INTEGER DEFAULT (unixepoch()),
                updated_at INTEGER DEFAULT (unixepoch())
            )",
            (),
        )
        .await?;
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
        )
        .await?;
        if version >= SchemaVersion::V0_6 {
            conn.execute(
                "CREATE TABLE fs_session_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )",
                (),
            )
            .await?;
        }

        insert_legacy_inode(conn, version, 1, S_IFDIR | 0o755, 2, 0).await?;
        insert_legacy_inode(conn, version, 2, S_IFREG | DEFAULT_FILE_MODE as i64, 1, 6).await?;
        insert_legacy_inode(conn, version, 3, S_IFREG | DEFAULT_FILE_MODE as i64, 1, 4).await?;
        conn.execute(
            "INSERT INTO fs_dentry (name, parent_ino, ino) VALUES
             ('file.txt', 1, 2),
             ('duplicate.txt', 1, 3)",
            (),
        )
        .await?;
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
            )
            .await?;
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
            )
            .await?;
            conn.execute(
                "INSERT INTO fs_op_journal (txn_id, op, payload, wallclock_ms)
                 VALUES (1, 'write', '{\"ino\":2,\"ranges\":[]}', 1)",
                (),
            )
            .await?;
            conn.execute(
                "INSERT INTO fs_journal_chunk (seq, digest) VALUES (1, ?)",
                (Value::Blob(abcd),),
            )
            .await?;
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
            )
            .await?;
        }
        conn.execute(
            "INSERT INTO kv_store (key, value) VALUES ('k', '{\"v\":1}')",
            (),
        )
        .await?;
        conn.execute(
            "INSERT INTO tool_calls (name, parameters, status, started_at) VALUES ('tool', '{}', 'success', 1)",
            (),
        )
        .await?;
        Ok(())
    }

    async fn insert_legacy_inode(
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
            values,
        )
        .await?;
        Ok(())
    }

    async fn read_fixture_file_bytes(conn: &Connection) -> Result<Vec<u8>> {
        let sql = if get_table_columns(conn, "fs_data")
            .await?
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
        let mut rows = conn.query(sql, ()).await?;
        let mut bytes = Vec::new();
        while let Some(row) = rows.next().await? {
            match row.get_value(0)? {
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
}
