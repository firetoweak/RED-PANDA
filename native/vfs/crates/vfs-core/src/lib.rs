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
pub mod schema;
pub mod semantics;
pub mod telemetry;

use error::{Error, Result};
use pool::{ConnectionPool, PooledConnection};
use std::path::{Path, PathBuf};
use turso::Builder;

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
pub struct Vfs {
    pool: ConnectionPool,
    pub fs: fs::Vfs,
}

impl Vfs {
    /// Open an immutable, digest-addressed Vfs artifact without modifying its
    /// SQLite file family.
    ///
    /// This constructor uses Turso's strict read-only open flags, applies only
    /// connection-local non-writing pragmas, validates the existing schema,
    /// and omits write batching and mount-orphan recovery. Its filesystem
    /// lifecycle barriers are no-ops, so reads and shutdown neither checkpoint
    /// nor remove `-wal`/`-shm` sidecars.
    ///
    /// Turso (verified through 0.7.2) caches databases by file identity
    /// without considering open flags. Therefore the same path must never be
    /// opened writable in this process. Artifact paths passed here must be immutable and
    /// digest-addressed, and this API must be their only in-process open path.
    pub async fn open_read_only(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        if !path.exists() {
            return Err(Error::DatabaseNotFound(path.display().to_string()));
        }
        let path_str = path
            .to_str()
            .ok_or_else(|| Error::InvalidUtf8Path(path.display().to_string()))?;
        let db = Builder::new_local(path_str).read_only(true).build().await?;
        let pool = ConnectionPool::with_options(
            db,
            fs::vfs::read_only_file_backed_connection_pool_options(),
        );
        let fs =
            fs::Vfs::from_read_only_pool(pool.clone(), path.to_path_buf(), CoreConfig::from_env())
                .await?;

        Ok(Self { pool, fs })
    }

    /// Open a filesystem at an explicit path, or in memory for ephemeral use.
    pub async fn open(options: VfsOptions) -> Result<Self> {
        let db_path = options.db_path()?;
        let core_config = options.core_config.unwrap_or_else(CoreConfig::from_env);
        let db = Builder::new_local(&db_path).build().await?;
        let pool = if db_path == ":memory:" {
            ConnectionPool::with_options(db, fs::vfs::memory_connection_pool_options())
        } else {
            ConnectionPool::with_options(db, fs::vfs::file_backed_connection_pool_options())
        };
        let db_path = (db_path != ":memory:").then(|| PathBuf::from(db_path));
        let fs =
            fs::Vfs::from_pool_with_path_and_config(pool.clone(), db_path, core_config).await?;
        Ok(Self { pool, fs })
    }

    /// Get a connection from the pool
    pub async fn get_connection(&self) -> Result<PooledConnection> {
        self.pool.get_connection().await
    }

    /// Get the connection pool
    pub fn get_pool(&self) -> ConnectionPool {
        self.pool.clone()
    }

    /// Capture an immutable root at the acknowledged journal head.
    pub async fn capture_root(&self, reason: &str) -> Result<SnapshotHeader> {
        self.fs.drain_all().await?;
        let conn = self.pool.get_connection().await?;
        let root = fs::history::capture_root(&conn, reason).await?;
        self.fs.journal_ctx().forget_chunks();
        Ok(root)
    }

    /// Return the retained replay range and complete transaction targets.
    pub async fn history_status(&self) -> Result<HistoryStatus> {
        let conn = self.pool.get_connection().await?;
        fs::history::status(&conn).await
    }

    /// Validate that `target_seq` is a complete, reconstructible history target.
    pub async fn validate_target(&self, target_seq: i64) -> Result<ValidatedHistoryTarget> {
        let conn = self.pool.get_connection().await?;
        fs::history::validate_target(&conn, target_seq).await
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
        let staging_path = staging_path.as_ref();
        if !staging_path.is_file() {
            return Err(Error::DatabaseNotFound(staging_path.display().to_string()));
        }
        let path = staging_path
            .to_str()
            .ok_or_else(|| Error::InvalidUtf8Path(staging_path.display().to_string()))?;
        let db = Builder::new_local(path).build().await?;
        let conn = db.connect()?;
        schema::ensure_current(&conn).await?;
        let info = fs::history::reconstruct(&conn, staging_path, target_seq).await?;
        let mut rows = conn.query("PRAGMA wal_checkpoint(TRUNCATE)", ()).await?;
        while rows.next().await?.is_some() {}
        Ok(info)
    }

    /// Establish the current state as a fresh generation-scoped history floor.
    pub async fn establish_history_floor(&self, reason: &str) -> Result<SnapshotHeader> {
        self.fs.drain_all().await?;
        let conn = self.pool.get_connection().await?;
        let root = fs::history::establish_fresh_floor(&conn, reason).await?;
        // Floor establishment collects unpinned chunks; drop cached digests
        // so no later commit pins one the collection removed.
        self.fs.journal_ctx().forget_chunks();
        Ok(root)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha256};
    use std::collections::BTreeMap;
    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;
    use std::path::Path;

    async fn create_frozen_artifact(temp_dir: &Path) -> PathBuf {
        let source_path = temp_dir.join("source.db");
        {
            let source = fs::Vfs::new(source_path.to_str().unwrap()).await.unwrap();
            let (_, file) =
                FileSystem::create_file(&source, 1, "artifact.txt", DEFAULT_FILE_MODE, 1000, 1000)
                    .await
                    .unwrap();
            file.pwrite(0, b"frozen artifact").await.unwrap();
            source.finalize().await.unwrap();
        }

        let artifact_path = temp_dir.join("artifact.db");
        std::fs::copy(source_path, &artifact_path).unwrap();
        artifact_path
    }

    fn file_family_snapshot(path: &Path) -> BTreeMap<String, Option<[u8; 32]>> {
        ["", "-wal", "-shm"]
            .into_iter()
            .map(|suffix| {
                let family_path = PathBuf::from(format!("{}{suffix}", path.display()));
                let hash = family_path.exists().then(|| {
                    let bytes = std::fs::read(&family_path).unwrap();
                    Sha256::digest(bytes).into()
                });
                (suffix.to_string(), hash)
            })
            .collect()
    }

    #[tokio::test]
    async fn test_vfs_creation() {
        let vfs = Vfs::open(VfsOptions::ephemeral()).await.unwrap();
        // Just verify we can get the connection
        let _conn = vfs.get_connection().await.unwrap();
    }

    #[tokio::test]
    async fn open_read_only_preserves_single_file_artifact_family() {
        let temp_dir = tempfile::tempdir().unwrap();
        let artifact_path = create_frozen_artifact(temp_dir.path()).await;
        let before = file_family_snapshot(&artifact_path);

        {
            let vfs = Vfs::open_read_only(&artifact_path).await.unwrap();
            let stats = FileSystem::lookup(&vfs.fs, 1, "artifact.txt")
                .await
                .unwrap()
                .unwrap();
            let file = FileSystem::open(&vfs.fs, stats.ino, libc::O_RDONLY)
                .await
                .unwrap();
            assert_eq!(file.pread(0, 64).await.unwrap(), b"frozen artifact");
            assert_eq!(
                FileSystem::readdir(&vfs.fs, 1).await.unwrap().unwrap(),
                vec!["artifact.txt"]
            );
            FileSystem::finalize(&vfs.fs).await.unwrap();
        }

        assert_eq!(file_family_snapshot(&artifact_path), before);
        assert!(!PathBuf::from(format!("{}-wal", artifact_path.display())).exists());
        assert!(!PathBuf::from(format!("{}-shm", artifact_path.display())).exists());
    }

    #[tokio::test]
    async fn open_read_only_rejects_filesystem_write_without_mutating_family() {
        let temp_dir = tempfile::tempdir().unwrap();
        let artifact_path = create_frozen_artifact(temp_dir.path()).await;
        let before = file_family_snapshot(&artifact_path);

        {
            let vfs = Vfs::open_read_only(&artifact_path).await.unwrap();
            let error = FileSystem::mkdir(&vfs.fs, 1, "forbidden", DEFAULT_DIR_MODE, 1000, 1000)
                .await
                .unwrap_err();
            assert!(
                matches!(error, Error::Database(turso::Error::Readonly(_))),
                "unexpected write error: {error:?}"
            );
            FileSystem::drain_all(&vfs.fs).await.unwrap();
        }

        assert_eq!(file_family_snapshot(&artifact_path), before);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn open_read_only_reads_chmod_0444_artifact() {
        let temp_dir = tempfile::tempdir().unwrap();
        let artifact_path = create_frozen_artifact(temp_dir.path()).await;
        std::fs::set_permissions(&artifact_path, std::fs::Permissions::from_mode(0o444)).unwrap();
        let before = file_family_snapshot(&artifact_path);

        {
            let vfs = Vfs::open_read_only(&artifact_path).await.unwrap();
            let stats = FileSystem::lookup(&vfs.fs, 1, "artifact.txt")
                .await
                .unwrap()
                .unwrap();
            let file = FileSystem::open(&vfs.fs, stats.ino, libc::O_RDONLY)
                .await
                .unwrap();
            assert_eq!(file.pread(0, 64).await.unwrap(), b"frozen artifact");
        }

        assert_eq!(file_family_snapshot(&artifact_path), before);
    }

    #[tokio::test]
    async fn test_filesystem_operations() {
        let vfs = Vfs::open(VfsOptions::ephemeral()).await.unwrap();

        // Create a directory
        vfs.fs.mkdir("/test_dir", 0, 0).await.unwrap();

        // Check directory exists
        let stats = vfs.fs.stat("/test_dir").await.unwrap();
        assert!(stats.is_some());
        let dir_stats = stats.unwrap();
        assert!(dir_stats.is_directory());

        // Write a file
        let data = b"Hello, Vfs!";
        let (_, file) = vfs
            .fs
            .create_file("/test_dir/test.txt", DEFAULT_FILE_MODE, 0, 0)
            .await
            .unwrap();
        file.pwrite(0, data).await.unwrap();

        // Read the file
        let read_data = vfs
            .fs
            .read_file("/test_dir/test.txt")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(read_data, data);

        // List directory
        let entries = vfs.fs.readdir(dir_stats.ino).await.unwrap().unwrap();
        assert_eq!(entries, vec!["test.txt"]);
    }

    #[test]
    fn test_db_path_is_absolute() {
        // Mount teardown chdirs the process to `/`; a relative db path handed
        // to turso would make every later by-path operation resolve wrong.
        let by_path = VfsOptions::with_path("some-dir/relative.db")
            .db_path()
            .unwrap();
        assert!(
            std::path::Path::new(&by_path).is_absolute(),
            "with_path must absolutize: {by_path}"
        );
        assert!(std::path::Path::new(&by_path).ends_with("some-dir/relative.db"));

        assert_eq!(VfsOptions::ephemeral().db_path().unwrap(), ":memory:");
        assert_eq!(
            VfsOptions::with_path(":memory:").db_path().unwrap(),
            ":memory:"
        );
    }
}
