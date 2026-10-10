//! Schema authority for Vfs databases.
//!
//! This module owns all production Rust DDL, schema-version detection, and
//! current-format initialization and validation.

pub mod integrity;

use crate::config::{DEFAULT_CHUNK_SIZE, DEFAULT_INLINE_THRESHOLD};
use crate::error::{Error, Result};
use tokio_rusqlite::rusqlite::{types::Value, Connection};
use tokio_rusqlite::rusqlite::{Transaction, TransactionBehavior};

/// Current schema version.
pub const CURRENT: SchemaVersion = SchemaVersion::V0_12;

/// Only the current local filesystem format is accepted.
pub const MIN_SUPPORTED: SchemaVersion = SchemaVersion::V0_12;

/// Current persisted format marker.
pub const VFS_SCHEMA_VERSION: &str = CURRENT.as_str();
pub const CONFIG_SCHEMA_VERSION_KEY: &str = "schema_version";
pub const CONFIG_CHUNK_SIZE_KEY: &str = "chunk_size";
pub const CONFIG_INLINE_THRESHOLD_KEY: &str = "inline_threshold";
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
    /// Current filesystem state; operation history belongs to Sandbox.
    V0_12,
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
            SchemaVersion::V0_12 => "0.12",
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
            SchemaVersion::V0_12 => 12,
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
            "0.12" => Some(SchemaVersion::V0_12),
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
            12 => Some(SchemaVersion::V0_12),
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
    "fs_origin",
    "fs_partial_origin",
    "fs_chunk_override",
];

/// Detect the schema version of an existing database.
///
/// Returns `None` if the database has no `fs_inode` table and is therefore a
/// new database from the schema authority's perspective.
pub fn detect_schema_version(conn: &Connection) -> Result<Option<SchemaVersion>> {
    let raw_user_version = user_version(conn)?;
    if raw_user_version > 0 {
        let version = SchemaVersion::from_user_version(raw_user_version).ok_or_else(|| {
            Error::SchemaVersionMismatch {
                found: format!("user_version {raw_user_version}"),
                expected: CURRENT.to_string(),
            }
        })?;
        return Ok(Some(version));
    }

    if !table_exists(conn, "fs_inode")? {
        return Ok(None);
    }

    if table_exists(conn, "fs_snapshot")? {
        return Ok(Some(SchemaVersion::V0_8));
    }

    if table_exists(conn, "fs_chunk")? {
        return Ok(Some(SchemaVersion::V0_7));
    }

    let columns = get_table_columns(conn, "fs_inode")?;
    let has_nlink = columns.iter().any(|c| c.name == "nlink");
    let has_atime_nsec = columns.iter().any(|c| c.name == "atime_nsec");
    let has_mtime_nsec = columns.iter().any(|c| c.name == "mtime_nsec");
    let has_ctime_nsec = columns.iter().any(|c| c.name == "ctime_nsec");
    let has_rdev = columns.iter().any(|c| c.name == "rdev");
    let has_data_inline = columns.iter().any(|c| c.name == "data_inline");
    let has_storage_kind = columns.iter().any(|c| c.name == "storage_kind");

    if has_data_inline && has_storage_kind && table_exists(conn, "fs_session_metadata")? {
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
pub fn check_schema_version(conn: &Connection) -> Result<()> {
    if let Some(version) = detect_schema_version(conn)? {
        if !version.is_current() {
            return Err(Error::SchemaVersionMismatch {
                found: version.to_string(),
                expected: CURRENT.to_string(),
            });
        }
        validate_current_schema(conn)?;
    }
    Ok(())
}

/// Initialize a new database or validate the current format. Older formats
/// are rejected before any DDL or data changes.
pub fn ensure_current(conn: &Connection) -> Result<()> {
    let raw_user_version = user_version(conn)?;
    let detected = detect_schema_version(conn)?;

    if let Some(version) = detected {
        if version < MIN_SUPPORTED {
            return Err(Error::SchemaVersionMismatch {
                found: version.to_string(),
                expected: CURRENT.to_string(),
            });
        }
    }
    if raw_user_version == CURRENT.user_version() {
        validate_current_schema(conn)?;
        ensure_current_indexes(conn)?;
        return Ok(());
    }
    if detected == Some(CURRENT) {
        filesystem_identity(conn)?;
    }

    let txn = Transaction::new_unchecked(conn, TransactionBehavior::Immediate)?;
    let result = (|| {
        execute_current_ddl(conn)?;
        if detected.is_none() {
            conn.execute(
                "INSERT INTO fs_config (key, value) VALUES (?, ?)",
                (CONFIG_FILESYSTEM_ID_KEY, uuid::Uuid::new_v4().to_string()),
            )?;
        }
        ensure_config_defaults(conn)?;
        set_user_version(conn, CURRENT)?;
        Ok(())
    })();

    match result {
        Ok(()) => txn.commit()?,
        Err(err) => {
            txn.rollback()?;
            return Err(err);
        }
    }

    validate_current_schema(conn)?;
    Ok(())
}

/// Set or update the overlay base-path marker without owning any DDL locally.
pub(crate) fn set_overlay_base_path(conn: &Connection, base_path: &str) -> Result<()> {
    ensure_current(conn)?;
    conn.execute(
        "INSERT OR REPLACE INTO fs_overlay_config (key, value) VALUES ('base_path', ?1)",
        [Value::Text(base_path.to_string())],
    )?;
    Ok(())
}

fn execute_current_ddl(conn: &Connection) -> Result<()> {
    for sql in ddl::create_all(CURRENT) {
        conn.execute(sql, [])?;
    }
    Ok(())
}

fn ensure_current_indexes(conn: &Connection) -> Result<()> {
    // Schema validation checks columns; ensure planner indexes also exist.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fs_dentry_parent ON fs_dentry(parent_ino, name)",
        [],
    )?;
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fs_dentry_parent_ino ON fs_dentry(parent_ino, ino)",
        [],
    )?;
    Ok(())
}

fn ensure_config_defaults(conn: &Connection) -> Result<()> {
    conn.execute(
        "INSERT OR REPLACE INTO fs_config (key, value) VALUES (?, ?)",
        (CONFIG_SCHEMA_VERSION_KEY, CURRENT.as_str()),
    )?;
    conn.execute(
        "INSERT OR IGNORE INTO fs_config (key, value) VALUES (?, ?)",
        (CONFIG_CHUNK_SIZE_KEY, DEFAULT_CHUNK_SIZE.to_string()),
    )?;
    // A defaulted inline threshold must fit the recorded chunk size.
    let chunk_size = read_config_value(conn, CONFIG_CHUNK_SIZE_KEY)?
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(DEFAULT_CHUNK_SIZE);
    conn.execute(
        "INSERT OR IGNORE INTO fs_config (key, value) VALUES (?, ?)",
        (
            CONFIG_INLINE_THRESHOLD_KEY,
            DEFAULT_INLINE_THRESHOLD.min(chunk_size).to_string(),
        ),
    )?;
    Ok(())
}

fn read_config_value(conn: &Connection, key: &str) -> Result<Option<String>> {
    let mut query_statement_3 = conn.prepare_cached("SELECT value FROM fs_config WHERE key = ?")?;
    let mut rows = query_statement_3.query((key,))?;
    if let Some(row) = rows.next()? {
        Ok(Some(row.get(0)?))
    } else {
        Ok(None)
    }
}

fn validate_current_schema(conn: &Connection) -> Result<()> {
    for table in REQUIRED_CURRENT_TABLES {
        if !table_exists(conn, table)? {
            return Err(Error::Internal(format!(
                "current schema is missing required table {table}"
            )));
        }
    }

    for spec in CURRENT_COLUMN_SPECS {
        ensure_column_matches(conn, *spec)?;
    }

    filesystem_identity(conn)?;

    Ok(())
}

/// Immutable creation namespace; never default missing or corrupt persisted identity.
pub(crate) fn filesystem_identity(conn: &Connection) -> Result<String> {
    let value = read_config_value(conn, CONFIG_FILESYSTEM_ID_KEY)?
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

fn user_version(conn: &Connection) -> Result<i64> {
    let mut query_statement_4 = conn.prepare_cached("PRAGMA user_version")?;
    let mut rows = query_statement_4.query([])?;
    let row = rows
        .next()?
        .ok_or_else(|| Error::Internal("PRAGMA user_version returned no rows".to_string()))?;
    row.get(0).map_err(Error::from)
}

fn set_user_version(conn: &Connection, version: SchemaVersion) -> Result<()> {
    conn.execute(
        &format!("PRAGMA user_version = {}", version.user_version()),
        [],
    )?;
    Ok(())
}

fn table_exists(conn: &Connection, table_name: &str) -> Result<bool> {
    let mut query_statement_5 =
        conn.prepare_cached("SELECT name FROM sqlite_master WHERE type='table' AND name=?")?;
    let mut rows = query_statement_5.query((table_name,))?;
    Ok(rows.next()?.is_some())
}

fn get_table_columns(conn: &Connection, table_name: &str) -> Result<Vec<ColumnInfo>> {
    let mut query_statement_6 =
        conn.prepare_cached(&format!("PRAGMA table_info({})", table_name))?;
    let mut rows = query_statement_6.query([])?;

    let mut columns = Vec::new();
    while let Some(row) = rows.next()? {
        let name: String = row.get(1)?;
        let type_name: String = row.get(2)?;
        let not_null: i64 = row.get(3)?;
        let default_value = match row.get::<_, Value>(4)? {
            Value::Text(value) => Some(value),
            Value::Integer(value) => Some(value.to_string()),
            Value::Null => None,
            value => Some(format!("{value:?}")),
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

fn ensure_column_matches(conn: &Connection, spec: ColumnSpec) -> Result<()> {
    let columns = get_table_columns(conn, spec.table_name)?;
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
#[path = "../../tests/internal/schema.rs"]
mod tests;
