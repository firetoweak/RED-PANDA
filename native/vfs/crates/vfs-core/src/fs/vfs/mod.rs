//! Vfs core facade and module spine.
//!
//! This module owns the shared `Vfs` state, connection setup, path
//! resolution helpers, cache invalidation hooks, and lifecycle spine. Focused
//! child modules implement caches, file handles, bulk import, path delegates,
//! and the canonical `FileSystem` trait implementation.

use crate::error::Error;
use crate::error::Result;
use std::path::PathBuf;
use std::sync::Arc;
#[cfg(test)]
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio_rusqlite::rusqlite::{types::Value, Connection};

use super::{
    BoxedFile, FilesystemStats, FsError, Stats, DEFAULT_DIR_MODE, MAX_NAME_LEN, S_IFDIR, S_IFLNK,
    S_IFMT, S_IFREG,
};
#[cfg(test)]
use super::{FileSystem, TimeChange, WriteRange, DEFAULT_FILE_MODE};
#[cfg(test)]
use crate::config::BatcherConfig;
use crate::config::{CoreConfig, Geometry};
use crate::pool::ConnectionPool;
use crate::schema;

mod batcher;
mod caches;
mod file;
mod fs;
mod import;
pub(crate) mod journal;
mod lifecycle;
mod path_api;
pub(in crate::fs) mod store;

use batcher::{
    BatcherDrain, BatcherPendingView, Drain, PendingGeneration, PendingView, VfsWriteBatcher,
};
use caches::{AttrCache, DentryCache, NegativeDentryCache};
pub use file::VfsFile;
pub use import::{ImportEntry, ImportOptions, ImportSession, ImportedEntry};
pub use journal::journal_gc;
pub(in crate::fs) use journal::JournalCtx;
pub(crate) use journal::{InodeRow, JournalDelta, MutationTxn, PartialOriginRow};
pub use lifecycle::ReapHook;
use lifecycle::{Lifecycle, OpenInodeGuard};

#[cfg(test)]
use store::{
    dense_after_inline_write_batch, normalize_write_ranges, NormalizedWriteRange, WriteRangeRef,
};

const ROOT_INO: i64 = 1;
const STORAGE_CHUNKED: i64 = 0;
const STORAGE_INLINE: i64 = 1;
const DENTRY_CACHE_MAX_SIZE: usize = 10000;
const NEGATIVE_DENTRY_CACHE_MAX_SIZE: usize = 10000;
const FILE_BACKED_MAX_CONNECTIONS: usize = 8;
const BASELINE_SYNCHRONOUS_SQL: &str = "PRAGMA synchronous = NORMAL";
const DURABLE_SYNCHRONOUS_SQL: &str = "PRAGMA synchronous = FULL";
const ATTR_CACHE_MAX_SIZE: usize = 10000;

/// A filesystem backed by SQLite
#[derive(Clone)]
pub struct Vfs {
    pool: ConnectionPool,
    db_path: Option<Arc<PathBuf>>,
    read_only: bool,
    filesystem_id: Arc<str>,
    chunk_size: usize,
    inline_threshold: usize,
    /// Cache for directory entry lookups (shared across clones)
    dentry_cache: Arc<DentryCache>,
    /// Cache for negative directory entry lookups (shared across clones)
    negative_dentry_cache: Arc<NegativeDentryCache>,
    /// Cache for inode attributes (shared across clones)
    attr_cache: Arc<AttrCache>,
    /// Synchronous pending view, safe to consult while a pooled connection is held.
    pending_view: Option<BatcherPendingView>,
    /// Async drain/enqueue surface. Code holding a pooled connection must not
    /// have access to this surface.
    write_drain: Option<BatcherDrain>,
    /// Concrete batcher retained only for white-box unit tests.
    write_batcher: Option<Arc<VfsWriteBatcher>>,
    /// Bulk-import transaction sizes observed by white-box tests.
    #[cfg(test)]
    import_commit_sizes: Arc<Mutex<Vec<usize>>>,
    /// Tier 4 escape hatch: when false (`VFS_OVERLAY_READS=0`), the SDK
    /// behaves like Tier 3 — every pwrite drains, every pread drains,
    /// `merge_pending_view` is a no-op. ON by default.
    overlay_reads: bool,
    /// Typed runtime configuration captured once when the filesystem opens.
    core_config: Arc<CoreConfig>,
    /// Kill-switch state plus the shared next-seq hint for journal commits.
    journal: journal::JournalCtx,
    /// Open-handle registry, deferred orphan queue, and reap hooks.
    lifecycle: Arc<Lifecycle>,
}

fn current_timestamp() -> Result<(i64, i64)> {
    let dur = SystemTime::now().duration_since(UNIX_EPOCH)?;
    Ok((dur.as_secs() as i64, dur.subsec_nanos() as i64))
}

impl Vfs {
    /// Create a new filesystem
    pub async fn new(db_path: &str) -> Result<Self> {
        let pool = if db_path == ":memory:" {
            ConnectionPool::memory()
        } else {
            ConnectionPool::writable(db_path, FILE_BACKED_MAX_CONNECTIONS as u32)
        };
        let db_path = if db_path == ":memory:" {
            None
        } else {
            Some(PathBuf::from(db_path))
        };
        Self::from_pool_with_path_and_config(pool, db_path, CoreConfig::from_env()).await
    }

    /// Create a filesystem from a connection pool
    pub async fn from_pool(pool: ConnectionPool) -> Result<Self> {
        Self::from_pool_with_config(pool, CoreConfig::from_env()).await
    }

    /// Create a filesystem from a connection pool and explicit core config.
    pub async fn from_pool_with_config(pool: ConnectionPool, config: CoreConfig) -> Result<Self> {
        Self::from_pool_with_path_and_config(pool, None, config).await
    }

    pub(crate) async fn from_read_only_pool(
        pool: ConnectionPool,
        db_path: PathBuf,
        mut config: CoreConfig,
    ) -> Result<Self> {
        let db_path = std::path::absolute(db_path)?;
        let (filesystem_id, chunk_size, inline_threshold) = pool
            .execute(|conn| {
                schema::check_schema_version(conn)?;
                Ok((
                    schema::filesystem_identity(conn)?,
                    Self::read_chunk_size(conn)?,
                    Self::read_inline_threshold(conn)?,
                ))
            })
            .await?;
        config.geometry = Geometry {
            chunk_size,
            inline_threshold,
        };

        Ok(Self {
            pool,
            db_path: Some(Arc::new(db_path)),
            read_only: true,
            filesystem_id: Arc::from(filesystem_id),
            chunk_size,
            inline_threshold,
            dentry_cache: Arc::new(DentryCache::new(DENTRY_CACHE_MAX_SIZE)),
            negative_dentry_cache: Arc::new(NegativeDentryCache::new(
                NEGATIVE_DENTRY_CACHE_MAX_SIZE,
            )),
            attr_cache: Arc::new(AttrCache::new(ATTR_CACHE_MAX_SIZE)),
            pending_view: None,
            write_drain: None,
            write_batcher: None,
            #[cfg(test)]
            import_commit_sizes: Arc::new(Mutex::new(Vec::new())),
            overlay_reads: config.overlay_reads,
            core_config: Arc::new(config),
            journal: journal::JournalCtx::new(false),
            lifecycle: Arc::new(Lifecycle::default()),
        })
    }

    pub(crate) async fn from_pool_with_path_and_config(
        pool: ConnectionPool,
        db_path: Option<PathBuf>,
        mut config: CoreConfig,
    ) -> Result<Self> {
        // finalize() resolves this path for sidecar removal long after the
        // caller may have changed the working directory (mount teardown
        // chdirs to `/`), so a relative path would silently miss the -wal.
        let db_path = db_path.map(std::path::absolute).transpose()?;
        let journal = journal::JournalCtx::new(config.journal_enabled);
        let lifecycle = Arc::new(Lifecycle::default());
        let setup_journal = journal.clone();
        let setup_lifecycle = lifecycle.clone();
        let journaling = config.journal_enabled;
        let (filesystem_id, chunk_size, inline_threshold) = pool
            .execute(move |conn| {
                Self::initialize_schema(conn, setup_journal.clone())?;
                let identity = schema::filesystem_identity(conn)?;
                super::history::reconcile_epoch(conn, journaling)?;
                setup_lifecycle.sweep_mount_orphans(conn, setup_journal)?;
                Ok((
                    identity,
                    Self::read_chunk_size(conn)?,
                    Self::read_inline_threshold(conn)?,
                ))
            })
            .await?;
        config.geometry = Geometry {
            chunk_size,
            inline_threshold,
        };
        let core_config = Arc::new(config);

        let attr_cache = Arc::new(AttrCache::new(ATTR_CACHE_MAX_SIZE));
        // Batching policy comes from typed core configuration.
        let (pending_view, write_drain, _write_batcher) = if core_config.batcher.enabled {
            let invalidate = {
                let attr_cache = Arc::clone(&attr_cache);
                Arc::new(move |ino| attr_cache.remove(ino)) as batcher::Invalidate
            };
            let batcher = Arc::new(VfsWriteBatcher::from_config(
                pool.clone(),
                chunk_size,
                inline_threshold,
                invalidate,
                &core_config.batcher,
                journal.clone(),
            ));
            let (pending_view, write_drain) = VfsWriteBatcher::split(&batcher);
            (Some(pending_view), Some(write_drain), Some(batcher))
        } else {
            (None, None, None)
        };

        let overlay_reads = core_config.overlay_reads;
        let fs = Self {
            pool,
            db_path: db_path.map(Arc::new),
            read_only: false,
            filesystem_id: Arc::from(filesystem_id),
            chunk_size,
            inline_threshold,
            dentry_cache: Arc::new(DentryCache::new(DENTRY_CACHE_MAX_SIZE)),
            negative_dentry_cache: Arc::new(NegativeDentryCache::new(
                NEGATIVE_DENTRY_CACHE_MAX_SIZE,
            )),
            attr_cache,
            pending_view,
            write_drain,
            write_batcher: _write_batcher,
            #[cfg(test)]
            import_commit_sizes: Arc::new(Mutex::new(Vec::new())),
            overlay_reads,
            core_config,
            journal,
            lifecycle,
        };
        Ok(fs)
    }

    /// Get the configured chunk size
    pub fn chunk_size(&self) -> usize {
        self.chunk_size
    }

    /// Get the configured inline threshold.
    pub(crate) fn inline_threshold(&self) -> usize {
        self.inline_threshold
    }

    pub fn core_config(&self) -> &CoreConfig {
        self.core_config.as_ref()
    }

    pub(crate) fn partial_origin_policy(&self) -> crate::fs::PartialOriginPolicy {
        self.core_config.partial_origin
    }

    pub(crate) fn journal_ctx(&self) -> journal::JournalCtx {
        self.journal.clone()
    }

    /// Configured journal retention horizon, in retained operations.
    pub fn journal_retention_ops(&self) -> usize {
        self.core_config.journal_retention_ops
    }

    pub(crate) fn register_reap_hook(&self, hook: Arc<dyn ReapHook>) -> bool {
        self.lifecycle.register_reap_hook(hook)
    }

    #[cfg(all(test, unix))]
    pub(crate) fn reap_hook_count(&self) -> usize {
        self.lifecycle.reap_hook_count()
    }

    /// Get the connection pool
    pub fn get_pool(&self) -> ConnectionPool {
        self.pool.clone()
    }

    /// Initialize the database schema
    fn initialize_schema(conn: &Connection, journal: journal::JournalCtx) -> Result<()> {
        schema::ensure_current(conn)?;
        let mut txn = MutationTxn::begin(conn, journal)?;

        // Ensure root directory exists with correct ownership
        let mut query_statement_0 =
            conn.prepare_cached("SELECT uid, gid FROM fs_inode WHERE ino = ?")?;
        let mut rows = query_statement_0.query((ROOT_INO,))?;
        let root_ownership = if let Some(row) = rows.next()? {
            Some((row.get::<_, u32>(0)?, row.get::<_, u32>(1)?))
        } else {
            None
        };
        drop(rows);

        // SAFETY: getuid/getgid are always safe
        #[cfg(unix)]
        let (uid, gid) = unsafe { (libc::getuid(), libc::getgid()) };
        #[cfg(not(unix))]
        let (uid, gid) = (0u32, 0u32);

        let changed = if root_ownership.is_none() {
            let dur = SystemTime::now().duration_since(UNIX_EPOCH)?;
            let now_secs = dur.as_secs() as i64;
            let now_nsec = dur.subsec_nanos() as i64;
            txn.conn().execute(
                "INSERT INTO fs_inode (ino, mode, nlink, uid, gid, size, atime, mtime, ctime, atime_nsec, mtime_nsec, ctime_nsec)
                VALUES (?, ?, 2, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
                (ROOT_INO, DEFAULT_DIR_MODE as i64, uid, gid, now_secs, now_secs, now_secs, now_nsec, now_nsec, now_nsec),
            )
            ?;
            schema::refresh_empty_initial_root(txn.conn())?;
            Some(InodeRow {
                ino: ROOT_INO,
                mode: DEFAULT_DIR_MODE as i64,
                nlink: 2,
                uid: uid as i64,
                gid: gid as i64,
                size: 0,
                atime: now_secs,
                mtime: now_secs,
                ctime: now_secs,
                rdev: 0,
                atime_nsec: now_nsec,
                mtime_nsec: now_nsec,
                ctime_nsec: now_nsec,
                data_inline: None,
                storage_kind: STORAGE_CHUNKED,
            })
        } else if root_ownership != Some((uid, gid)) {
            // Update existing root inode ownership to current user
            let mut query_statement_1 = conn.prepare_cached(
                "UPDATE fs_inode SET uid = ?, gid = ? WHERE ino = ?
                     RETURNING ino, mode, nlink, uid, gid, size, atime, mtime, ctime, rdev,
                               atime_nsec, mtime_nsec, ctime_nsec, data_inline, storage_kind",
            )?;
            let mut rows = query_statement_1.query((uid, gid, ROOT_INO))?;
            let row = rows.next()?.ok_or_else(|| {
                Error::Internal("root ownership update returned no row".to_string())
            })?;
            Some(InodeRow::from_row(row, 0)?)
        } else {
            None
        };

        // A no-change open only read; committing it would count as a
        // mutating commit (and, with journaling disabled, durably mark the
        // history epoch invalid for a maintenance open that touched nothing).
        match changed {
            Some(root) => {
                txn.record_inode("root_init", root)?;
                txn.commit()?;
            }
            None => txn.rollback()?,
        }
        Ok(())
    }

    /// Read chunk size from config
    fn read_chunk_size(conn: &Connection) -> Result<usize> {
        let value: Value = conn.query_row(
            "SELECT value FROM fs_config WHERE key = 'chunk_size'",
            [],
            |row| row.get(0),
        )?;
        let decoded = match value {
            Value::Text(text) => text
                .parse::<usize>()
                .map_err(|_| FsError::Corrupt("invalid chunk_size".into()))?,
            Value::Integer(value) => {
                usize::try_from(value).map_err(|_| FsError::Corrupt("invalid chunk_size".into()))?
            }
            _ => return Err(FsError::Corrupt("invalid chunk_size type".into()).into()),
        };
        if decoded == 0 {
            return Err(FsError::Corrupt("zero chunk_size".into()).into());
        }
        Ok(decoded)
    }

    /// Read inline threshold from config
    fn read_inline_threshold(conn: &Connection) -> Result<usize> {
        let value: Value = conn.query_row(
            "SELECT value FROM fs_config WHERE key = 'inline_threshold'",
            [],
            |row| row.get(0),
        )?;
        let decoded = match value {
            Value::Text(text) => text
                .parse::<usize>()
                .map_err(|_| FsError::Corrupt("invalid inline_threshold".into()))?,
            Value::Integer(value) => usize::try_from(value)
                .map_err(|_| FsError::Corrupt("invalid inline_threshold".into()))?,
            _ => return Err(FsError::Corrupt("invalid inline_threshold type".into()).into()),
        };
        Ok(decoded)
    }

    /// Normalize a path
    fn normalize_path(&self, path: &str) -> String {
        let normalized = path.trim_end_matches('/');
        let normalized = if normalized.is_empty() {
            "/"
        } else if normalized.starts_with('/') {
            normalized
        } else {
            return format!("/{}", normalized);
        };

        // Handle . and .. components
        let components: Vec<&str> = normalized.split('/').filter(|s| !s.is_empty()).collect();
        let mut result = Vec::new();

        for component in components {
            match component {
                "." => {
                    // Current directory - skip it
                    continue;
                }
                ".." => {
                    // Parent directory - only pop if there is a component to pop (don't traverse above root)
                    if !result.is_empty() {
                        result.pop();
                    }
                }
                _ => {
                    result.push(component);
                }
            }
        }

        if result.is_empty() {
            "/".to_string()
        } else {
            format!("/{}", result.join("/"))
        }
    }

    /// Split path into components
    fn split_path(&self, path: &str) -> Vec<String> {
        let normalized = self.normalize_path(path);
        if normalized == "/" {
            return vec![];
        }
        normalized
            .split('/')
            .filter(|p| !p.is_empty())
            .map(|s| s.to_string())
            .collect()
    }

    /// Look up a child entry by parent inode and name using a provided connection.
    ///
    /// This is more efficient than `resolve_path` when you already have the parent inode,
    /// as it avoids re-resolving all parent path components.
    fn lookup_child(&self, conn: &Connection, parent_ino: i64, name: &str) -> Result<Option<i64>> {
        if let Some(cached_ino) = self.dentry_cache.get(parent_ino, name) {
            return Ok(Some(cached_ino));
        }
        if self.negative_dentry_cache.contains(parent_ino, name) {
            return Ok(None);
        }

        let mut stmt =
            conn.prepare_cached("SELECT ino FROM fs_dentry WHERE parent_ino = ? AND name = ?")?;
        let mut rows = stmt.query((parent_ino, name))?;

        let mut found_ino = None;
        let mut row_count = 0;

        while let Some(row) = rows.next()? {
            found_ino = Some(Some(row.get::<_, i64>(0)?).ok_or_else(|| {
                FsError::Corrupt(format!(
                    "invalid ino for dentry {parent_ino}/{name}: expected integer"
                ))
            })?);
            row_count += 1;
        }

        if row_count > 1 {
            return Err(FsError::InvalidPath.into());
        }

        if let Some(ino) = found_ino {
            self.cache_dentry(parent_ino, name, ino);
        } else {
            self.cache_negative_dentry(parent_ino, name);
        }

        Ok(found_ino)
    }

    fn cache_attr(&self, stats: Stats) {
        self.attr_cache.insert(stats);
    }

    fn pending_generation(&self, ino: i64) -> Option<PendingGeneration> {
        self.pending_view
            .as_ref()
            .map(|view| view.pending_generation(ino))
    }

    fn cache_attr_if_pending_generation(
        &self,
        stats: Stats,
        generation: Option<PendingGeneration>,
    ) {
        if let (Some(view), Some(generation)) = (&self.pending_view, generation) {
            if view.pending_generation(stats.ino) != generation {
                return;
            }
        }
        self.cache_attr(stats);
    }

    pub(crate) fn invalidate_attr(&self, ino: i64) {
        self.attr_cache.remove(ino);
    }

    /// Drain pending batched writes for one inode.
    async fn drain_inode_writes(&self, ino: i64) -> Result<()> {
        if let Some(drain) = &self.write_drain {
            drain.drain_inode(ino).await?;
        }
        Ok(())
    }

    /// Prelude shared by chmod / chown / utimens.
    ///
    /// Legacy drain-on-setattr behaviour synchronously commits the inode's
    /// pending batched writes so the deferred data commit can never re-stamp
    /// mtime/ctime after the explicit attribute change. With FUSE writeback
    /// caching the kernel issues one SETATTR per written file, so that drain
    /// serialised a SQLite commit per file on the clone path.
    ///
    /// Default: skip the drain and instead mark the pending entry so the
    /// eventual batched commit preserves mtime/ctime (`mark_times_explicit` /
    /// `preserve_times`). The mark happens BEFORE the caller's fs_inode
    /// UPDATE; combined with the commit path re-reading the flag after it
    /// holds the SQLite write lock, the explicitly-set attributes win in every
    /// interleaving.
    ///
    /// The deferral requires Tier-4 overlay reads: with
    /// overlay reads disabled, getattr/size are served straight from
    /// SQLite with no pending-size merge, so the legacy drain is kept to make
    /// the just-written size visible at close time (git reads files by
    /// `st_size`).
    async fn prepare_attr_change(&self, ino: i64) -> Result<()> {
        if self.core_config.drain_on_setattr || !self.overlay_reads {
            return self.drain_inode_writes(ino).await;
        }
        if let Some(drain) = &self.write_drain {
            drain.mark_times_explicit(ino);
        }
        Ok(())
    }

    /// Tier Four helper: merge the batcher's pending state into a `Stats` row
    /// read from SQLite, so callers that hold a pool connection don't need to
    /// drain (which would deadlock on single-conn pools):
    /// - `size` is OR-ed with the pending max write end (mirrors the logic in
    ///   `Vfs::getattr` and `VfsFile::pread`);
    /// - explicitly-set times stashed by `utimens` (`PendingTimeChange`) are
    ///   overlaid so a deferred SETATTR is visible before its drain commits.
    ///
    /// Fast-paths when the batcher has nothing pending for this inode (Tier 4
    /// read hot path: most reads pay zero cost beyond a read-lock HashMap hit).
    fn merge_pending_view(&self, ino: i64, stats: Option<&mut Stats>) {
        let Some(stats) = stats else {
            return;
        };
        // Escape hatch: when overlay reads are disabled, callers' SQLite
        // size view is already authoritative because pwrites went straight
        // to SQLite (see VfsFile::pwrite) and utimens never stashes.
        // No merge needed.
        if !self.overlay_reads {
            return;
        }
        let Some(view) = &self.pending_view else {
            return;
        };
        view.merge_into_stats(ino, stats);
    }

    /// Drain all pending batched writes for this Vfs instance.
    pub async fn drain_all(&self) -> Result<()> {
        self.check_background()?;

        if self.read_only {
            return Ok(());
        }
        if let Some(drain) = &self.write_drain {
            drain.drain_all().await?;
        }

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;

                owned.pool.checkpoint(conn)?;
                Ok(())
            })
            .await
    }

    /// Stop batch scheduling, reap closed orphans and checkpoint committed writes.
    pub async fn finalize(&self) -> Result<()> {
        if self.read_only {
            return self.pool.barrier().await;
        }
        if let Some(batcher) = &self.write_batcher {
            batcher.shutdown().await?;
        }
        self.process_deferred_reaps().await?;
        self.drain_all().await?;
        self.pool.barrier().await?;
        Ok(())
    }

    /// Reap inodes whose deletion unlink/rename deferred because open
    /// handles existed (POSIX unlink-while-open). Runs opportunistically at
    /// namespace mutations and at finalize; a crash is covered by the
    /// nlink=0 sweep at mount.
    pub(crate) async fn process_deferred_reaps(&self) -> Result<()> {
        self.check_background()?;
        if !self.lifecycle.has_pending_reaps() {
            return Ok(());
        }
        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let reaped =
                    owned
                        .lifecycle
                        .process_deferred_reaps(conn, owned.journal_ctx(), |ino| {
                            owned.discard_pending_for_reaped_inode(ino);
                        })?;
                for ino in reaped {
                    owned.invalidate_attr(ino);
                }
                Ok(())
            })
            .await
    }

    fn reap_inode_with_conn(
        &self,
        conn: &Connection,
        ino: i64,
    ) -> Result<Option<lifecycle::ReapChanges>> {
        self.lifecycle.reap_inode_with_conn(conn, ino)
    }

    /// Drop batcher state for an inode that is being reaped. Inline unlink /
    /// rename-replace callers invoke this only after their metadata
    /// transaction commits, so a reap-hook rollback leaves pending writes
    /// intact with the still-live inode. Deferred reaps call it before the
    /// transaction opens because the inode is already nlink=0 and invisible.
    fn discard_pending_for_reaped_inode(&self, ino: i64) {
        if let Some(drain) = &self.write_drain {
            drain.discard_pending(ino);
        }
    }

    fn invalidate_parent_attr(&self, parent_ino: i64) {
        self.invalidate_attr(parent_ino);
    }

    fn invalidate_dentry(&self, parent_ino: i64, name: &str) {
        self.dentry_cache.remove(parent_ino, name);
        self.negative_dentry_cache.remove(parent_ino, name);
    }

    fn cache_dentry(&self, parent_ino: i64, name: &str, child_ino: i64) {
        self.negative_dentry_cache.remove(parent_ino, name);
        self.dentry_cache.insert(parent_ino, name, child_ino);
    }

    fn cache_negative_dentry(&self, parent_ino: i64, name: &str) {
        self.dentry_cache.remove(parent_ino, name);
        self.negative_dentry_cache.insert(parent_ino, name);
    }

    pub(crate) fn create_file_with_conn(
        &self,
        conn: &Connection,
        parent_ino: i64,
        name: &str,
        mode: u32,
        ownership: (u32, u32),
        update_parent_times: bool,
    ) -> Result<(Stats, Option<InodeRow>, i64)> {
        let (uid, gid) = ownership;
        let dur = SystemTime::now().duration_since(UNIX_EPOCH)?;
        let now_secs = dur.as_secs() as i64;
        let now_nsec = dur.subsec_nanos() as i64;
        let file_mode = S_IFREG | (mode & 0o7777);

        let mut inode_stmt = conn
            .prepare_cached(
                "INSERT INTO fs_inode (mode, nlink, uid, gid, size, atime, mtime, ctime, atime_nsec, mtime_nsec, ctime_nsec, data_inline, storage_kind)
                 VALUES (?, 1, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING ino",
            )
            ?;
        let mut single_row_query = inode_stmt.query((
            file_mode as i64,
            uid,
            gid,
            now_secs,
            now_secs,
            now_secs,
            now_nsec,
            now_nsec,
            now_nsec,
            Value::Blob(Vec::new()),
            STORAGE_INLINE,
        ))?;
        let row = single_row_query.next()?.ok_or(FsError::NotFound)?;
        let ino = Some(row.get::<_, i64>(0)?)
            .ok_or_else(|| Error::Internal("failed to get inode".to_string()))?;

        match conn.execute(
            "INSERT INTO fs_dentry (name, parent_ino, ino) VALUES (?, ?, ?)",
            (name, parent_ino, ino),
        ) {
            Ok(_) => {}
            Err(tokio_rusqlite::rusqlite::Error::SqliteFailure(code, _))
                if code.code == tokio_rusqlite::rusqlite::ErrorCode::ConstraintViolation =>
            {
                return Err(FsError::AlreadyExists.into())
            }
            Err(error) => return Err(error.into()),
        }
        let dentry_id = conn.last_insert_rowid();

        let parent = if update_parent_times {
            let mut query_statement_0 = conn.prepare_cached(
                "UPDATE fs_inode
                     SET ctime = ?, mtime = ?, ctime_nsec = ?, mtime_nsec = ?
                     WHERE ino = ?
                     RETURNING ino, mode, nlink, uid, gid, size, atime, mtime, ctime, rdev,
                               atime_nsec, mtime_nsec, ctime_nsec, data_inline, storage_kind",
            )?;
            let mut rows =
                query_statement_0.query((now_secs, now_secs, now_nsec, now_nsec, parent_ino))?;
            let row = rows.next()?.ok_or_else(|| {
                Error::Internal("create parent update returned no row".to_string())
            })?;
            Some(InodeRow::from_row(row, 0)?)
        } else {
            None
        };

        Ok((
            Stats {
                ino,
                mode: file_mode,
                nlink: 1,
                uid,
                gid,
                size: 0,
                atime: now_secs,
                mtime: now_secs,
                ctime: now_secs,
                atime_nsec: now_nsec as u32,
                mtime_nsec: now_nsec as u32,
                ctime_nsec: now_nsec as u32,
                rdev: 0,
            },
            parent,
            dentry_id,
        ))
    }

    pub(crate) fn publish_created_file(&self, parent_ino: i64, name: &str, stats: &Stats) {
        self.cache_dentry(parent_ino, name, stats.ino);
        self.invalidate_parent_attr(parent_ino);
        self.cache_attr(stats.clone());
    }

    /// Get link count for an inode
    fn get_link_count(&self, conn: &Connection, ino: i64) -> Result<u32> {
        store::link_count(conn, ino)
    }

    /// Get file attributes by inode using an existing connection
    fn getattr_with_conn(&self, conn: &Connection, ino: i64) -> Result<Option<Stats>> {
        if let Some(stats) = self.attr_cache.get(ino) {
            return Ok(Some(stats));
        }

        let generation = self.pending_generation(ino);
        if let Some(mut stats) = store::getattr(conn, ino)? {
            self.merge_pending_view(ino, Some(&mut stats));
            self.cache_attr_if_pending_generation(stats.clone(), generation);
            Ok(Some(stats))
        } else {
            Ok(None)
        }
    }

    /// Resolve a path to an inode number
    async fn resolve_path(&self, path: &str) -> Result<Option<i64>> {
        self.check_background()?;

        let path = path.to_owned();

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;
                let path = path.as_str();

                owned.resolve_path_with_conn(conn, path)
            })
            .await
    }

    /// Resolve a path to an inode number using a provided connection
    fn resolve_path_with_conn(&self, conn: &Connection, path: &str) -> Result<Option<i64>> {
        let components = self.split_path(path);
        crate::telemetry::record_path_resolution(components.len() as u64);
        if components.is_empty() {
            return Ok(Some(ROOT_INO));
        }

        let mut statement: Option<tokio_rusqlite::rusqlite::CachedStatement<'_>> = None;
        let mut current_ino = ROOT_INO;
        for component in components {
            // Check cache first
            if let Some(cached_ino) = self.dentry_cache.get(current_ino, &component) {
                current_ino = cached_ino;
                continue;
            }
            if self.negative_dentry_cache.contains(current_ino, &component) {
                crate::telemetry::record_negative_lookup();
                return Ok(None);
            }

            // Cache miss - query database
            if statement.is_none() {
                statement = Some(conn.prepare_cached(
                    "SELECT ino FROM fs_dentry WHERE parent_ino = ? AND name = ?",
                )?);
            }
            let statement = statement.as_mut().expect("statement was set above");
            let mut rows = statement.query((current_ino, component.as_str()))?;

            let mut found_row = None;
            let mut row_count = 0;

            while let Some(row) = rows.next()? {
                found_row = Some(row.get::<_, i64>(0)?);
                row_count += 1;
            }

            if row_count > 1 {
                return Err(FsError::InvalidPath.into());
            }

            if let Some(row) = found_row {
                let child_ino = row;

                // Populate cache
                self.cache_dentry(current_ino, &component, child_ino);
                current_ino = child_ino;
            } else {
                crate::telemetry::record_negative_lookup();
                self.cache_negative_dentry(current_ino, &component);
                return Ok(None);
            }
        }

        Ok(Some(current_ino))
    }

    /// Resolve a path to its parent directory inode and final component name.
    ///
    /// This is the canonical parent/name resolver backing every path-based
    /// mutation helper; external path consumers
    /// must use it rather than re-deriving parent inodes.
    pub async fn resolve_parent_and_name(&self, path: &str) -> Result<(i64, String)> {
        let path = self.normalize_path(path);
        let components = self.split_path(&path);
        if components.is_empty() {
            return Err(FsError::RootOperation.into());
        }

        let parent_path = match components.len() {
            1 => "/".to_string(),
            _ => format!("/{}", components[..components.len() - 1].join("/")),
        };
        let parent_ino = self
            .resolve_path(&parent_path)
            .await?
            .ok_or(FsError::NotFound)?;
        let name = components.last().cloned().ok_or(FsError::InvalidPath)?;
        Ok((parent_ino, name))
    }

    /// List directory contents
    pub async fn readdir(&self, ino: i64) -> Result<Option<Vec<String>>> {
        self.check_background()?;

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;

                let mut query_statement_0 = conn.prepare_cached(
                    "SELECT name FROM fs_dentry WHERE parent_ino = ? ORDER BY name",
                )?;
                let mut rows = query_statement_0.query((ino,))?;

                let mut entries = Vec::new();
                while let Some(row) = rows.next()? {
                    let name = row.get::<_, String>(0)?;
                    if !name.is_empty() {
                        entries.push(name);
                    }
                }

                Ok(Some(entries))
            })
            .await
    }

    /// Read the target of a symbolic link
    pub async fn readlink(&self, path: &str) -> Result<Option<String>> {
        self.check_background()?;

        let path = path.to_owned();

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;
                let path = path.as_str();

                owned.readlink_with_conn(conn, path)
            })
            .await
    }

    /// Read the target of a symbolic link using a provided connection
    fn readlink_with_conn(&self, conn: &Connection, path: &str) -> Result<Option<String>> {
        let path = self.normalize_path(path);

        let ino = match self.resolve_path_with_conn(conn, &path)? {
            Some(ino) => ino,
            None => return Ok(None),
        };

        // Check if it's a symlink by querying the inode
        if let Some(mode) = store::mode(conn, ino)? {
            if (mode & S_IFMT) != S_IFLNK {
                return Err(FsError::NotASymlink.into());
            }
        } else {
            return Ok(None);
        }

        // Read target from fs_symlink table
        let mut query_statement_0 =
            conn.prepare_cached("SELECT target FROM fs_symlink WHERE ino = ?")?;
        let mut rows = query_statement_0.query((ino,))?;

        if let Some(row) = rows.next()? {
            let target = row.get::<_, String>(0)?;
            Ok(Some(target))
        } else {
            Ok(None)
        }
    }

    /// Get filesystem statistics
    ///
    /// Returns the total number of inodes and bytes used by file contents.
    async fn statfs(&self) -> Result<FilesystemStats> {
        self.check_background()?;

        self.drain_all().await?;

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;

                // Count total inodes
                let mut stmt = conn.prepare_cached("SELECT COUNT(*) FROM fs_inode")?;
                let mut rows = stmt.query([])?;

                let inodes = if let Some(row) = rows.next()? {
                    row.get::<_, i64>(0)? as u64
                } else {
                    0
                };

                // Sum total bytes used (from file sizes in inodes)
                let mut stmt =
                    conn.prepare_cached("SELECT COALESCE(SUM(size), 0) FROM fs_inode")?;
                let mut rows = stmt.query([])?;

                let bytes_used = if let Some(row) = rows.next()? {
                    row.get::<_, i64>(0)? as u64
                } else {
                    0
                };

                Ok(FilesystemStats { inodes, bytes_used })
            })
            .await
    }

    /// Open a file and return a file handle.
    ///
    /// The returned handle can be used for efficient read/write/fsync operations
    /// without requiring path lookups on each operation.
    pub async fn open(&self, path: &str) -> Result<BoxedFile> {
        let path = self.normalize_path(path);
        let ino = self.resolve_path(&path).await?.ok_or(FsError::NotFound)?;

        Ok(Arc::new(VfsFile {
            pool: self.pool.clone(),
            ino,
            chunk_size: self.chunk_size,
            inline_threshold: self.inline_threshold,
            attr_cache: self.attr_cache.clone(),
            pending_view: self.pending_view.clone(),
            write_drain: self.write_drain.clone(),
            overlay_reads: self.overlay_reads,
            journal: self.journal_ctx(),
            _open_guard: Some(Arc::new(self.lifecycle.guard(ino))),
        }))
    }

    /// Get the number of chunks for a given inode (for testing).
    /// Drains any pending batched writes first so the returned count reflects
    /// the full committed state — Tier 4 deferred SQLite commits until fsync
    /// or timer, so tests that inspect `fs_data` directly need a sync point.
    #[cfg(test)]
    async fn get_chunk_count(&self, ino: i64) -> Result<i64> {
        self.check_background()?;

        self.drain_inode_writes(ino).await?;

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;

                let mut query_statement_0 =
                    conn.prepare_cached("SELECT COUNT(*) FROM fs_data WHERE ino = ?")?;
                let mut rows = query_statement_0.query((ino,))?;

                if let Some(row) = rows.next()? {
                    Ok(row.get::<_, i64>(0)?)
                } else {
                    Ok(0)
                }
            })
            .await
    }

    #[cfg(test)]
    async fn get_storage_state(&self, ino: i64) -> Result<(i64, Option<Vec<u8>>)> {
        self.check_background()?;

        self.drain_inode_writes(ino).await?;

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;

                let mut query_statement_0 = conn.prepare_cached(
                    "SELECT storage_kind, data_inline FROM fs_inode WHERE ino = ?",
                )?;
                let mut rows = query_statement_0.query((ino,))?;

                if let Some(row) = rows.next()? {
                    let storage_kind = row.get::<_, i64>(0)?;
                    let data_inline = match row.get::<_, Value>(1) {
                        Ok(Value::Blob(data)) => Some(data),
                        _ => None,
                    };
                    Ok((storage_kind, data_inline))
                } else {
                    Err(FsError::NotFound.into())
                }
            })
            .await
    }
}

#[cfg(test)]
#[path = "../../../tests/internal/vfs.rs"]
mod vfs_tests;

impl Vfs {
    fn check_background(&self) -> Result<()> {
        self.pool.check_ready()?;
        if let Some(drain) = &self.write_drain {
            drain.check_completed()?;
        }
        Ok(())
    }
}
