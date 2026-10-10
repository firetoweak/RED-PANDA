//! Error types for the Vfs SDK.

use thiserror::Error;

/// The main error type for the Vfs SDK.
///
/// Wrapper variants chain their cause through `source()` only and keep it out
/// of `Display`: `#[from]` already exposes the inner error to reporters that
/// walk the chain (anyhow `{:#}`), so repeating `{0}` in the message would
/// print every cause twice ("database error: X: X").
#[derive(Debug, Error)]
pub enum Error {
    /// Database error from SQLite
    #[error("database error")]
    Database(#[from] tokio_rusqlite::rusqlite::Error),

    /// IO error
    #[error("io error")]
    Io(#[from] std::io::Error),

    /// JSON serialization/deserialization error
    #[error("json error")]
    Json(#[from] serde_json::Error),

    /// System time error
    #[error("time error")]
    Time(#[from] std::time::SystemTimeError),

    /// Filesystem-specific error with errno semantics
    #[error(transparent)]
    Fs(#[from] crate::fs::FsError),

    /// Database file path does not exist
    #[error("database not found: {0}")]
    DatabaseNotFound(String),

    /// Invalid path encoding
    #[error("path '{0}' is not valid UTF-8")]
    InvalidUtf8Path(String),

    /// Base directory does not exist
    #[error("base directory does not exist: {0}")]
    BaseDirectoryNotFound(String),

    /// Path is not a directory
    #[error("path is not a directory: {0}")]
    NotADirectory(String),

    /// Connection pool timeout - no connections available
    #[error("connection pool timeout: no connections available")]
    ConnectionPoolTimeout,

    /// Internal error (for unexpected conditions)
    #[error("{0}")]
    Internal(String),

    /// Schema version mismatch - database schema version doesn't match expected version
    #[error("schema version mismatch: database is version {found}, expected {expected}")]
    SchemaVersionMismatch { found: String, expected: String },
}

/// Result type alias using the SDK Error type.
pub type Result<T> = std::result::Result<T, Error>;
