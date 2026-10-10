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

use error::{Error, Result};
use pool::ConnectionPool;
use std::path::{Path, PathBuf};

// Re-export filesystem types
pub use config::{
    BatcherConfig, CoreConfig, EnvReader, Geometry, DEFAULT_JOURNAL_RETENTION_OPS,
    DEFAULT_WRITE_BATCH_BYTES, DEFAULT_WRITE_BATCH_GLOBAL_BYTES, DEFAULT_WRITE_BATCH_MS,
    DEFAULT_WRITE_BATCH_TXN_BYTES, DEFAULT_WRITE_BATCH_TXN_INODES,
};
#[cfg(any(target_os = "linux", target_os = "macos", windows))]
pub use fs::HostFS;
pub use fs::{
    journal_gc, BoxedFile, DirEntry, File, FileSystem, FilesystemStats, FsError, HistoryStatus,
    HistoryTarget, ImportEntry, ImportOptions, ImportSession, ImportedEntry, OverlayFS,
    PartialOriginMode, PartialOriginPolicy, ReconstructionInfo, SnapshotHeader, Stats, TimeChange,
    ValidatedHistoryTarget, WriteRange, DEFAULT_DIR_MODE, DEFAULT_FILE_MODE,
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

    /// Capture an immutable root at the acknowledged journal head.
    pub async fn capture_root(&self, reason: &str) -> Result<SnapshotHeader> {
        self.check_background()?;

        self.fs.drain_all().await?;
        let reason = reason.to_owned();

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;
                let reason = reason.as_str();

                let root = fs::history::capture_root(conn, reason)?;
                owned.fs.journal_ctx().forget_chunks();
                Ok(root)
            })
            .await
    }

    /// Return the retained replay range and complete transaction targets.
    pub async fn history_status(&self) -> Result<HistoryStatus> {
        self.check_background()?;

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;

                fs::history::status(conn)
            })
            .await
    }

    /// Validate that `target_seq` is a complete, reconstructible history target.
    pub async fn validate_target(&self, target_seq: i64) -> Result<ValidatedHistoryTarget> {
        self.check_background()?;

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;

                fs::history::validate_target(conn, target_seq)
            })
            .await
    }

    /// Replace the filesystem state in a private staged database with a target.
    ///
    /// The path must name a caller-owned staging copy that is not open
    /// elsewhere in this process. This constructor deliberately bypasses the
    /// normal writable-open epoch transition: replay validates and transforms
    /// the staged copy's already-recorded durable history markers.
    pub async fn reconstruct_to(
        staging_path: impl AsRef<Path>,
        target_seq: i64,
    ) -> Result<ReconstructionInfo> {
        let staging_path = staging_path.as_ref().to_owned();
        if !staging_path.is_file() {
            return Err(Error::DatabaseNotFound(staging_path.display().to_string()));
        }
        let pool = ConnectionPool::writable(staging_path.clone(), 1);
        let result = pool
            .execute(move |conn| {
                schema::ensure_current(conn)?;
                let info = fs::history::reconstruct(conn, &staging_path, target_seq)?;
                artifacts::checkpoint_truncate(conn)?;
                Ok(info)
            })
            .await?;
        pool.close().await?;
        Ok(result)
    }

    /// Establish the current state as a fresh generation-scoped history floor.
    pub async fn establish_history_floor(&self, reason: &str) -> Result<SnapshotHeader> {
        self.check_background()?;

        self.fs.drain_all().await?;
        let reason = reason.to_owned();

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;
                let reason = reason.as_str();

                let root = fs::history::establish_fresh_floor(conn, reason)?;
                // Floor establishment collects unpinned chunks; drop cached digests
                // so no later commit pins one the collection removed.
                owned.fs.journal_ctx().forget_chunks();
                Ok(root)
            })
            .await
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
