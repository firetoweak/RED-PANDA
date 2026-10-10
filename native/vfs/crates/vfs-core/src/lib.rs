//! Vfs core: the SQLite-backed virtual filesystem engine.
//!
//! This is the only externally consumed crate. It owns the storage engine
//! (chunk/inline layout per docs/SPEC.md), the write batcher, inode
//! lifecycle and reap hooks, the overlay layer (whiteouts, origin tracking,
//! the partial-origin policy), scoped host-FS reads, schema authority
//! (current-format validation plus the integrity battery), the typed config
//! system parsed at the crate edge (`config::EnvReader`), the telemetry
//! registry with its single report sink, and the `semantics` facade
//! (access, durability, handles) that the transport adapter crates build on.
//!
//! Owned invariants:
//!
//! - All virtual filesystem state lives in the single Vfs SQLite
//!   database. Sandboxed writes never touch the host filesystem; overlay
//!   reads are scoped to the configured read-only base directory.
//! - Buffered (volatile-ack) writes are acceleration state only: durable
//!   acks (`AckDurability::Committed`, commit barriers, shutdown finalize)
//!   return only after the bytes are committed to SQLite, metadata reads
//!   merge pending state, and deletions discard it.
//! - Errors are typed (`FsError` at the trait, `Error` above it); row
//!   decoding never fabricates defaults for corrupt data.
//! - Environment variables are read only inside `config`; everything
//!   downstream receives values.

mod artifacts;
pub mod config;
pub mod error;
pub mod fs;
pub mod options;
pub mod pool;
mod scheduler;
pub mod schema;
pub mod semantics;
pub mod telemetry;

use error::Result;
use pool::ConnectionPool;
use std::path::{Path, PathBuf};

// Re-export filesystem types
pub use config::{
    BatcherConfig, CoreConfig, EnvReader, Geometry, DEFAULT_WRITE_BATCH_BYTES,
    DEFAULT_WRITE_BATCH_GLOBAL_BYTES, DEFAULT_WRITE_BATCH_MS, DEFAULT_WRITE_BATCH_TXN_BYTES,
    DEFAULT_WRITE_BATCH_TXN_INODES,
};
#[cfg(any(target_os = "linux", target_os = "macos", windows))]
pub use fs::HostFS;
pub use fs::{
    BoxedFile, DirEntry, File, FileSystem, FilesystemStats, FsError, ImportEntry, ImportOptions,
    ImportSession, ImportedEntry, OverlayFS, PartialOriginMode, PartialOriginPolicy, Stats,
    TimeChange, WriteRange, DEFAULT_DIR_MODE, DEFAULT_FILE_MODE,
    DEFAULT_PARTIAL_ORIGIN_THRESHOLD_BYTES, S_IFBLK, S_IFCHR, S_IFDIR, S_IFIFO, S_IFLNK, S_IFMT,
    S_IFREG, S_IFSOCK,
};
pub use options::VfsOptions;
pub use schema::{SchemaVersion, CURRENT, VFS_SCHEMA_VERSION};
pub use semantics::{AckDurability, Semantics, WriteReceipt};

/// The main Vfs SDK struct
///
/// Filesystem storage and immutable artifact operations backed by SQLite.
#[derive(Clone)]
pub struct Vfs {
    pool: ConnectionPool,
    pub fs: fs::Vfs,
}

impl Vfs {
    /// Open an immutable, digest-addressed Vfs artifact without modifying its
    /// SQLite file family.
    ///
    /// The input must be a frozen single-file artifact. SQLite immutable mode
    /// reads it without creating WAL/SHM or running writable recovery.
    pub async fn open_read_only(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        let pool = ConnectionPool::frozen(path, 8)?;
        let fs =
            fs::Vfs::from_read_only_pool(pool.clone(), path.to_owned(), CoreConfig::from_env())
                .await?;
        Ok(Self { pool, fs })
    }

    /// Open a filesystem at an explicit path, or in memory for ephemeral use.
    pub async fn open(options: VfsOptions) -> Result<Self> {
        let db_path = options.db_path()?;
        let core_config = options.core_config.unwrap_or_else(CoreConfig::from_env);
        let pool = if db_path == ":memory:" {
            ConnectionPool::memory()
        } else {
            ConnectionPool::writable(db_path.clone(), 8)
        };
        let db_path = (db_path != ":memory:").then(|| PathBuf::from(db_path));
        let fs =
            fs::Vfs::from_pool_with_path_and_config(pool.clone(), db_path, core_config).await?;
        Ok(Self { pool, fs })
    }

    /// Get the connection pool
    pub fn get_pool(&self) -> ConnectionPool {
        self.pool.clone()
    }
}

#[cfg(test)]
#[path = "../tests/internal/sdk.rs"]
mod tests;

impl Vfs {
    fn check_background(&self) -> Result<()> {
        self.pool.check_ready()
    }
}
#[global_allocator]
static ALLOCATOR: mimalloc::MiMalloc = mimalloc::MiMalloc;
