//! Canonical `FileSystem` trait implementation for Vfs.
//!
//! This module is the only Vfs mutation implementation. Path helpers,
//! FUSE and WinFsp resolve into these inode-oriented operations so
//! namespace, metadata, lifecycle, and batcher semantics cannot diverge.

use async_trait::async_trait;
use std::collections::HashSet;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio_rusqlite::rusqlite::{Transaction, TransactionBehavior};

use tokio_rusqlite::rusqlite::types::Value;

use crate::error::Error;
use crate::fs::{DirEntry, DirEntryPage, FileSystem, TimeChange};

use super::batcher::PendingTimeChange;
use super::*;

#[async_trait]
impl FileSystem for Vfs {
    fn file_identity(&self, ino: i64) -> Result<String> {
        Ok(format!("vfs:{}:{ino}", self.filesystem_id))
    }

    async fn lookup(&self, parent_ino: i64, name: &str) -> Result<Option<Stats>> {
        self.check_background()?;

        crate::telemetry::record_lookup();
        if name.len() > MAX_NAME_LEN {
            return Err(FsError::NameTooLong.into());
        }

        // Connection-free fast paths via the in-memory caches. These are the
        // same caches (and invalidation semantics) that `lookup_child` already
        // trusts; consulting them BEFORE acquiring a pool connection avoids a
        // wasted acquire/release on every cache hit. This is the clone hot
        // path: `OverlayFS::resolve_delta_parent` does O(depth) negative
        // delta-parent probes per base-layer lookup, all of which are negative
        // cache hits that previously each took a connection.
        if name != ".." {
            if self.negative_dentry_cache.contains(parent_ino, name) {
                crate::telemetry::record_negative_lookup();
                return Ok(None);
            }
            if let Some(child_ino) = self.dentry_cache.get(parent_ino, name) {
                if let Some(mut stats) = self.attr_cache.get(child_ino) {
                    self.merge_pending_view(child_ino, Some(&mut stats));
                    return Ok(Some(stats));
                }
            }
        }

        let name = name.to_owned();

        let owned = self.clone();
        self.pool.execute(move |conn| {
        let _keepalive = &owned;
        let name = name.as_str();

        // Handle ".." by finding the parent of parent_ino
        if name == ".." {
            if parent_ino == ROOT_INO {
                // Root's parent is itself
                return owned.getattr_with_conn(conn, ROOT_INO);
            }
            let mut stmt = conn
                .prepare_cached("SELECT parent_ino FROM fs_dentry WHERE ino = ? LIMIT 1")
                ?;
            let mut rows = stmt.query((parent_ino,))?;
            let parent = if let Some(row) = rows.next()? {
                row.get::<_, i64>(0)?
            } else {
                ROOT_INO
            };
            return owned.getattr_with_conn(conn, parent);
        }

        // Look up the child inode
        let child_ino = match owned.lookup_child(conn, parent_ino, name)? {
            Some(ino) => ino,
            None => {
                crate::telemetry::record_negative_lookup();
                return Ok(None);
            }
        };
        let generation = owned.pending_generation(child_ino);
        // Tier Four: do NOT call `drain_inode_writes` here. The single-
        // connection ephemeral pool (and even the file-backed pool under
        // contention) would deadlock — we already hold the only connection
        // permit, and `drain_inode_writes` -> `drain_pending_batched` tries
        // to acquire one. Read SQLite, then merge the batcher's pending
        // max-end into the size field the same way `getattr` does.

        // Get stats for the child inode
        let mut stmt = conn
            .prepare_cached("SELECT ino, mode, nlink, uid, gid, size, atime, mtime, ctime, rdev, atime_nsec, mtime_nsec, ctime_nsec FROM fs_inode WHERE ino = ?")
            ?;
        let mut rows = stmt.query((child_ino,))?;

        if let Some(row) = rows.next()? {
            let mut stats = store::stats_from_row(row)?;
            owned.merge_pending_view(child_ino, Some(&mut stats));
            // Cache the lookup result
            owned.cache_dentry(parent_ino, name, child_ino);
            owned.cache_attr_if_pending_generation(stats.clone(), generation);
            Ok(Some(stats))
        } else {
            Ok(None)
        }
            }).await
    }

    async fn getattr(&self, ino: i64) -> Result<Option<Stats>> {
        self.check_background()?;

        crate::telemetry::record_getattr();
        // Connection-free fast path: an attr-cache hit needs no pool connection.
        // The cache is invalidated on every write (enqueue removes the entry),
        // so a hit means there is no uncommitted pending write to merge; the
        // merge below is therefore an idempotent no-op but is kept for safety.
        // Same cache `getattr_with_conn` already trusts, consulted before the
        // acquire.
        if let Some(mut stats) = self.attr_cache.get(ino) {
            self.merge_pending_view(ino, Some(&mut stats));
            return Ok(Some(stats));
        }
        // Tier Four: don't drain — read SQLite metadata and OR in the
        // batcher's peek_pending_max_end so the size view reflects pending
        // writes that haven't been committed yet. Refresh the attr cache
        // with the merged size so subsequent direct cache reads agree with
        // what we just returned.

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;

                owned.getattr_with_conn(conn, ino)
            })
            .await
    }

    /// DB-backed regular files qualify for `FOPEN_KEEP_CACHE`: every mutation
    /// path through a mount is kernel-originated (the kernel's pages stay
    /// coherent for its own writes) and the adapter's fingerprint guard
    /// revalidates mtime/ctime/size at each open, so out-of-band SDK writers
    /// are caught exactly like external edits to host-backed base files.
    /// The keepcache-delta kill switch restores the old policy where only
    /// host-backed base-layer files were eligible.
    async fn keep_cache_for_read_open(&self, ino: i64, flags: i32) -> Result<Option<Stats>> {
        self.check_background()?;

        if (flags & (libc::O_WRONLY | libc::O_RDWR)) != libc::O_RDONLY
            || (flags & libc::O_TRUNC) != 0
        {
            return Ok(None);
        }
        if !self.core_config.keepcache_delta {
            return Ok(None);
        }
        let Some(stats) = FileSystem::getattr(self, ino).await? else {
            return Ok(None);
        };
        Ok(stats.is_file().then_some(stats))
    }

    fn delta_keep_cache_fast_path(&self) -> bool {
        self.core_config.keepcache_delta
    }

    fn external_watch_ignored_paths(&self) -> Vec<PathBuf> {
        self.db_path
            .as_ref()
            .map(|path| vec![path.as_ref().clone()])
            .unwrap_or_default()
    }

    async fn readlink(&self, ino: i64) -> Result<Option<String>> {
        self.check_background()?;

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;

                // Check if the inode exists and is a symlink
                if let Some(mode) = store::mode(conn, ino)? {
                    if (mode & S_IFMT) != S_IFLNK {
                        return Err(FsError::NotASymlink.into());
                    }
                } else {
                    return Ok(None);
                }

                // Read target from fs_symlink table
                let mut stmt =
                    conn.prepare_cached("SELECT target FROM fs_symlink WHERE ino = ?")?;
                let mut rows = stmt.query((ino,))?;

                if let Some(row) = rows.next()? {
                    let target = row.get::<_, String>(0)?;
                    Ok(Some(target))
                } else {
                    Ok(None)
                }
            })
            .await
    }

    async fn readdir(&self, ino: i64) -> Result<Option<Vec<String>>> {
        self.check_background()?;

        crate::telemetry::record_readdir();

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;

                // Check if inode exists and is a directory
                if let Some(mode) = store::mode(conn, ino)? {
                    if (mode & S_IFMT) != super::S_IFDIR {
                        return Err(FsError::NotADirectory.into());
                    }
                } else {
                    return Ok(None);
                }

                let mut stmt = conn.prepare_cached(
                    "SELECT name FROM fs_dentry WHERE parent_ino = ? ORDER BY name",
                )?;
                let mut rows = stmt.query((ino,))?;

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

    async fn readdir_plus(&self, ino: i64) -> Result<Option<Vec<DirEntry>>> {
        self.check_background()?;

        crate::telemetry::record_readdir_plus();
        self.drain_all().await?;

        let owned = self.clone();
        self.pool.execute(move |conn| {
        let _keepalive = &owned;

        // Check if inode exists and is a directory
        if let Some(mode) = store::mode(conn, ino)? {
            if (mode & S_IFMT) != super::S_IFDIR {
                return Err(FsError::NotADirectory.into());
            }
        } else {
            return Ok(None);
        }

        let mut stmt = conn.prepare_cached("SELECT d.id, d.name, i.ino, i.mode, i.nlink, i.uid, i.gid, i.size, i.atime, i.mtime, i.ctime, i.rdev, i.atime_nsec, i.mtime_nsec, i.ctime_nsec
            FROM fs_dentry d
            JOIN fs_inode i ON d.ino = i.ino
            WHERE d.parent_ino = ?
            ORDER BY d.name"
        )?;
        let mut rows = stmt.query((ino,))?;

        let mut entries = Vec::new();
        while let Some(row) = rows.next()? {
            let cookie = row.get::<_, i64>(0)?;
            let name = row.get::<_, String>(1)?;

            if name.is_empty() {
                continue;
            }

            let stats = store::stats_from_row_at(row, 2)?;

            owned.cache_attr(stats.clone());
            entries.push(DirEntry {
                name,
                stats,
                cookie,
            });
        }

        Ok(Some(entries))
            }).await
    }

    async fn readdir_plus_after(
        &self,
        ino: i64,
        start_after: i64,
        max_entries: usize,
    ) -> Result<Option<DirEntryPage>> {
        self.check_background()?;

        crate::telemetry::record_readdir_plus();
        self.drain_all().await?;

        let owned = self.clone();
        self.pool.execute(move |conn| {
        let _keepalive = &owned;

        // Check if inode exists and is a directory
        if let Some(mode) = store::mode(conn, ino)? {
            if (mode & S_IFMT) != super::S_IFDIR {
                return Err(FsError::NotADirectory.into());
            }
        } else {
            return Ok(None);
        }

        let start_after_name = if start_after > 0 {
            let mut stmt = conn
                .prepare_cached("SELECT name FROM fs_dentry WHERE id = ? AND parent_ino = ?")
                ?;
            let mut rows = stmt.query((start_after, ino))?;
            match rows.next()? {
                Some(row) => Some(
                    row.get::<_, String>(0)?,
                ),
                None => return Err(FsError::BadCookie.into()),
            }
        } else {
            None
        };

        let fetch_limit = max_entries.saturating_add(1).min(i64::MAX as usize) as i64;
        let mut stmt = if start_after_name.is_some() {
            conn.prepare_cached(
                "SELECT d.id, d.name, i.ino, i.mode, i.nlink, i.uid, i.gid, i.size, i.atime, i.mtime, i.ctime, i.rdev, i.atime_nsec, i.mtime_nsec, i.ctime_nsec
                FROM fs_dentry d
                JOIN fs_inode i ON d.ino = i.ino
                WHERE d.parent_ino = ? AND d.name > ?
                ORDER BY d.parent_ino, d.name
                LIMIT ?",
            )
            ?
        } else {
            conn.prepare_cached(
                "SELECT d.id, d.name, i.ino, i.mode, i.nlink, i.uid, i.gid, i.size, i.atime, i.mtime, i.ctime, i.rdev, i.atime_nsec, i.mtime_nsec, i.ctime_nsec
                FROM fs_dentry d
                JOIN fs_inode i ON d.ino = i.ino
                WHERE d.parent_ino = ?
                ORDER BY d.parent_ino, d.name
                LIMIT ?",
            )
            ?
        };

        let mut rows = if let Some(start_after_name) = start_after_name {
            stmt.query((ino, start_after_name, fetch_limit))?
        } else {
            stmt.query((ino, fetch_limit))?
        };

        let mut entries = Vec::new();
        while let Some(row) = rows.next()? {
            let cookie = row.get::<_, i64>(0)?;
            let name = row.get::<_, String>(1)?;

            if name.is_empty() {
                continue;
            }

            let stats = store::stats_from_row_at(row, 2)?;

            owned.cache_attr(stats.clone());
            entries.push(DirEntry {
                name,
                stats,
                cookie,
            });
        }

        let end = entries.len() <= max_entries;
        entries.truncate(max_entries);
        Ok(Some(DirEntryPage { entries, end }))
            }).await
    }

    async fn chmod(&self, ino: i64, mode: u32) -> Result<()> {
        self.check_background()?;

        self.prepare_attr_change(ino).await?;

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;

                // BEGIN IMMEDIATE so this serialises with concurrent batcher drain
                // transactions instead of racing them as an autocommit statement
                // and waiting on SQLite's writer lock.
                let txn = Transaction::new_unchecked(conn, TransactionBehavior::Immediate)?;
                let result: Result<()> = (|| {
                    // Get current mode to preserve file type bits
                    let current_mode = store::mode(conn, ino)?.ok_or(FsError::NotFound)?;

                    // Preserve file type bits (upper bits), replace permission bits (lower 12 bits)
                    let new_mode = (current_mode & S_IFMT) | (mode & 0o7777);

                    let dur = SystemTime::now().duration_since(UNIX_EPOCH)?;
                    let now_secs = dur.as_secs() as i64;
                    let now_nsec = dur.subsec_nanos() as i64;
                    let mut query_statement_0 = conn.prepare_cached(
                        "UPDATE fs_inode SET mode = ?, ctime = ?, ctime_nsec = ? WHERE ino = ?
                     RETURNING ino",
                    )?;
                    let mut rows =
                        query_statement_0.query((new_mode as i64, now_secs, now_nsec, ino))?;
                    rows.next()?.ok_or(FsError::NotFound)?;
                    Ok(())
                })();

                match result {
                    Ok(()) => {
                        txn.commit()?;
                        owned.invalidate_attr(ino);
                        Ok(())
                    }
                    Err(error) => {
                        txn.rollback()?;
                        Err(error)
                    }
                }
            })
            .await
    }

    async fn chown(&self, ino: i64, uid: Option<u32>, gid: Option<u32>) -> Result<()> {
        self.check_background()?;

        if uid.is_none() && gid.is_none() {
            return Ok(());
        }
        self.prepare_attr_change(ino).await?;

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;

                // BEGIN IMMEDIATE: see `chmod` — avoid autocommit write/write races
                // with concurrent batcher drain transactions.
                let txn = Transaction::new_unchecked(conn, TransactionBehavior::Immediate)?;
                let result: Result<()> = (|| {
                    // Verify inode exists
                    let mut stmt = conn.prepare_cached("SELECT ino FROM fs_inode WHERE ino = ?")?;
                    let mut rows = stmt.query((ino,))?;

                    if rows.next()?.is_none() {
                        return Err(FsError::NotFound.into());
                    }

                    // Build the update query dynamically based on which values are provided
                    let mut updates = Vec::new();
                    let mut values: Vec<Value> = Vec::new();

                    if let Some(uid) = uid {
                        updates.push("uid = ?");
                        values.push(Value::Integer(uid as i64));
                    }
                    if let Some(gid) = gid {
                        updates.push("gid = ?");
                        values.push(Value::Integer(gid as i64));
                    }

                    let dur = SystemTime::now().duration_since(UNIX_EPOCH)?;
                    let now_secs = dur.as_secs() as i64;
                    let now_nsec = dur.subsec_nanos() as i64;
                    updates.push("ctime = ?");
                    values.push(Value::Integer(now_secs));
                    updates.push("ctime_nsec = ?");
                    values.push(Value::Integer(now_nsec));

                    values.push(Value::Integer(ino));
                    let sql = format!("UPDATE fs_inode SET {} WHERE ino = ?", updates.join(", "));
                    let sql = format!("{} RETURNING ino", sql);
                    let mut query_statement_0 = conn.prepare_cached(&sql)?;
                    let mut rows = query_statement_0
                        .query(tokio_rusqlite::rusqlite::params_from_iter(values))?;
                    rows.next()?.ok_or(FsError::NotFound)?;
                    Ok(())
                })();

                match result {
                    Ok(()) => {
                        txn.commit()?;
                        owned.invalidate_attr(ino);
                        Ok(())
                    }
                    Err(error) => {
                        txn.rollback()?;
                        Err(error)
                    }
                }
            })
            .await
    }

    async fn utimens(&self, ino: i64, atime: TimeChange, mtime: TimeChange) -> Result<()> {
        self.check_background()?;

        if matches!(atime, TimeChange::Omit) && matches!(mtime, TimeChange::Omit) {
            return Ok(());
        }

        // Group-commit fast path: with FUSE writeback caching the kernel sends
        // one SETATTR (mtime) per freshly written file, usually while that
        // file's data is pending in the write batcher (and sometimes after it
        // already drained). Instead of paying a dedicated SQLite transaction
        // per file for the time UPDATE, stash the resolved values in the
        // inode's pending entry (created on demand) — the batcher commits them
        // inside its next drain transaction (`apply_pending_times_with_conn`),
        // and `merge_pending_view` overlays them onto getattr/lookup results so
        // the change is visible immediately. Falls through to the direct
        // (transaction-wrapped) UPDATE when overlay reads are disabled or the
        // legacy drain is requested.
        if !self.core_config.drain_on_setattr && self.overlay_reads {
            if let Some(drain) = &self.write_drain {
                let dur = SystemTime::now().duration_since(UNIX_EPOCH)?;
                let now = (dur.as_secs() as i64, dur.subsec_nanos() as i64);
                let resolve = |tc: TimeChange| -> Option<(i64, i64)> {
                    match tc {
                        TimeChange::Set(secs, nsec) => Some((secs, nsec as i64)),
                        TimeChange::Now => Some(now),
                        TimeChange::Omit => None,
                    }
                };
                let change = PendingTimeChange {
                    atime: resolve(atime),
                    mtime: resolve(mtime),
                    // utimens always bumps ctime.
                    ctime: Some(now),
                };
                drain.stash_times(ino, change);
                self.invalidate_attr(ino);
                return Ok(());
            }
        }

        self.prepare_attr_change(ino).await?;

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;

                // BEGIN IMMEDIATE: see `chmod` — avoid autocommit write/write races
                // with concurrent batcher drain transactions.
                let txn = Transaction::new_unchecked(conn, TransactionBehavior::Immediate)?;
                let result: Result<()> = (|| {
                    // Verify inode exists
                    let mut stmt = conn.prepare_cached("SELECT ino FROM fs_inode WHERE ino = ?")?;
                    let mut rows = stmt.query((ino,))?;
                    if rows.next()?.is_none() {
                        return Err(FsError::NotFound.into());
                    }

                    let mut updates = Vec::new();
                    let mut values: Vec<Value> = Vec::new();

                    let resolve = |tc: TimeChange| -> (i64, i64) {
                        match tc {
                            TimeChange::Set(secs, nsec) => (secs, nsec as i64),
                            TimeChange::Now => {
                                let dur = SystemTime::now().duration_since(UNIX_EPOCH).unwrap();
                                (dur.as_secs() as i64, dur.subsec_nanos() as i64)
                            }
                            TimeChange::Omit => unreachable!(),
                        }
                    };

                    if !matches!(atime, TimeChange::Omit) {
                        let (secs, nsec) = resolve(atime);
                        updates.push("atime = ?");
                        values.push(Value::Integer(secs));
                        updates.push("atime_nsec = ?");
                        values.push(Value::Integer(nsec));
                    }

                    if !matches!(mtime, TimeChange::Omit) {
                        let (secs, nsec) = resolve(mtime);
                        updates.push("mtime = ?");
                        values.push(Value::Integer(secs));
                        updates.push("mtime_nsec = ?");
                        values.push(Value::Integer(nsec));
                    }

                    // Also update ctime
                    let dur = SystemTime::now().duration_since(UNIX_EPOCH)?;
                    updates.push("ctime = ?");
                    values.push(Value::Integer(dur.as_secs() as i64));
                    updates.push("ctime_nsec = ?");
                    values.push(Value::Integer(dur.subsec_nanos() as i64));

                    values.push(Value::Integer(ino));
                    let sql = format!("UPDATE fs_inode SET {} WHERE ino = ?", updates.join(", "));
                    let sql = format!("{} RETURNING ino", sql);
                    let mut query_statement_0 = conn.prepare_cached(&sql)?;
                    let mut rows = query_statement_0
                        .query(tokio_rusqlite::rusqlite::params_from_iter(values))?;
                    rows.next()?.ok_or(FsError::NotFound)?;
                    Ok(())
                })();

                match result {
                    Ok(()) => {
                        txn.commit()?;
                        owned.invalidate_attr(ino);
                        Ok(())
                    }
                    Err(error) => {
                        txn.rollback()?;
                        Err(error)
                    }
                }
            })
            .await
    }

    async fn open(&self, ino: i64, _flags: i32) -> Result<BoxedFile> {
        self.check_background()?;

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;

                // Verify inode exists
                let mut stmt = conn.prepare_cached("SELECT ino FROM fs_inode WHERE ino = ?")?;
                let mut rows = stmt.query((ino,))?;

                if rows.next()?.is_none() {
                    return Err(FsError::NotFound.into());
                }

                Ok(Arc::new(VfsFile {
                    pool: owned.pool.clone(),
                    ino,
                    chunk_size: owned.chunk_size,
                    inline_threshold: owned.inline_threshold,
                    attr_cache: owned.attr_cache.clone(),
                    pending_view: owned.pending_view.clone(),
                    write_drain: owned.write_drain.clone(),
                    overlay_reads: owned.overlay_reads,

                    _open_guard: Some(Arc::new(owned.lifecycle.guard(ino))),
                }) as BoxedFile)
            })
            .await
    }

    async fn mkdir(
        &self,
        parent_ino: i64,
        name: &str,
        mode: u32,
        uid: u32,
        gid: u32,
    ) -> Result<Stats> {
        self.check_background()?;

        if name.len() > MAX_NAME_LEN {
            return Err(FsError::NameTooLong.into());
        }
        let name = name.to_owned();

        let owned = self.clone();
        self.pool.execute(move |conn| {
        let _keepalive = &owned;
        let name = name.as_str();

        // BEGIN IMMEDIATE: see `chmod` — multi-statement metadata mutations
        // must not run as autocommit statements that race the write batcher's
        // drain transactions by waiting on SQLite's writer lock.
        let txn = Transaction::new_unchecked(conn, TransactionBehavior::Immediate)?;
        let result: Result<Stats> = (|| {
            // Check if already exists
            if owned.lookup_child(conn, parent_ino, name)?.is_some() {
                return Err(FsError::AlreadyExists.into());
            }

            // Create inode
            let dur = SystemTime::now().duration_since(UNIX_EPOCH)?;
            let now_secs = dur.as_secs() as i64;
            let now_nsec = dur.subsec_nanos() as i64;
            let mut stmt = conn
                .prepare_cached(
                    "INSERT INTO fs_inode (mode, uid, gid, size, atime, mtime, ctime, atime_nsec, mtime_nsec, ctime_nsec)
                    VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?) RETURNING ino",
                )
                ?;
            let dir_mode = super::S_IFDIR | (mode & 0o7777);
            let mut single_row_query = stmt.query((
                    dir_mode as i64,
                    uid,
                    gid,
                    now_secs,
                    now_secs,
                    now_secs,
                    now_nsec,
                    now_nsec,
                    now_nsec,
                ))
                ?;
let row = single_row_query.next()?.ok_or(FsError::NotFound)?;

            let ino = Some(row.get::<_, i64>(0)?)
                .ok_or_else(|| Error::Internal("failed to get inode".to_string()))?;

            // Create directory entry
            let mut stmt = conn
                .prepare_cached("INSERT INTO fs_dentry (name, parent_ino, ino) VALUES (?, ?, ?)")
                ?;
            stmt.execute((name, parent_ino, ino))?;

            // Set nlink to 2 for new directory (owned "." + parent's dentry)
            let mut stmt = conn
                .prepare_cached("UPDATE fs_inode SET nlink = 2 WHERE ino = ?")
                ?;
            stmt.execute((ino,))?;

            // Increment parent nlink (new directory's ".." link) and update timestamps
            let mut query_statement_0 = conn.prepare_cached("UPDATE fs_inode
                     SET nlink = nlink + 1, ctime = ?, mtime = ?, ctime_nsec = ?, mtime_nsec = ?
                     WHERE ino = ?
                     RETURNING ino")?;
let mut rows = query_statement_0.query((now_secs, now_secs, now_nsec, now_nsec, parent_ino))
                ?;
            rows.next()?.ok_or(FsError::NotFound)?;

            Ok(Stats {
                    ino,
                    mode: dir_mode,
                    nlink: 2,
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
                })
        })();

        match result {
            Ok(stats) => {

                txn.commit()?;
                // Populate dentry cache only after the transaction is durable.
                owned.cache_dentry(parent_ino, name, stats.ino);
                owned.invalidate_parent_attr(parent_ino);
                owned.cache_attr(stats.clone());
                Ok(stats)
            }
            Err(error) => {
                txn.rollback()?;
                Err(error)
            }
        }
            }).await
    }

    async fn create_file(
        &self,
        parent_ino: i64,
        name: &str,
        mode: u32,
        uid: u32,
        gid: u32,
    ) -> Result<(Stats, BoxedFile)> {
        self.check_background()?;

        if name.len() > MAX_NAME_LEN {
            return Err(FsError::NameTooLong.into());
        }
        let name = name.to_owned();

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;
                let name = name.as_str();

                // No existence pre-check: fs_dentry's UNIQUE(parent_ino, name) makes
                // the dentry INSERT below the authoritative collision detector (its
                // Constraint error maps to AlreadyExists and the transaction drop
                // rolls back the inode row). Saves one SELECT on the synchronous
                // create path that every git-clone file pays.

                let txn = Transaction::new_unchecked(conn, TransactionBehavior::Immediate)?;

                // Parent mtime/ctime: stash into the batcher overlay (committed by the
                // next group drain, served immediately via merge_pending_view) instead
                // of paying an UPDATE on the synchronous create path. Falls back to
                // the in-transaction UPDATE when the overlay cannot serve reads.
                let stash_parent_times = owned.overlay_reads && owned.write_drain.is_some();
                let stats = owned.create_file_with_conn(
                    &txn,
                    parent_ino,
                    name,
                    mode,
                    (uid, gid),
                    !stash_parent_times,
                )?;
                let ino = stats.ino;

                txn.commit()?;

                if stash_parent_times {
                    if let Some(drain) = &owned.write_drain {
                        drain.stash_times(
                            parent_ino,
                            PendingTimeChange {
                                atime: None,
                                mtime: Some((stats.mtime, stats.mtime_nsec as i64)),
                                ctime: Some((stats.ctime, stats.ctime_nsec as i64)),
                            },
                        );
                    }
                }

                owned.cache_dentry(parent_ino, name, ino);
                owned.invalidate_parent_attr(parent_ino);

                owned.cache_attr(stats.clone());

                let file: BoxedFile = Arc::new(VfsFile {
                    pool: owned.pool.clone(),
                    ino,
                    chunk_size: owned.chunk_size,
                    inline_threshold: owned.inline_threshold,
                    attr_cache: owned.attr_cache.clone(),
                    pending_view: owned.pending_view.clone(),
                    write_drain: owned.write_drain.clone(),
                    overlay_reads: owned.overlay_reads,

                    _open_guard: Some(Arc::new(owned.lifecycle.guard(ino))),
                });

                Ok((stats, file))
            })
            .await
    }

    async fn mknod(
        &self,
        parent_ino: i64,
        name: &str,
        mode: u32,
        rdev: u64,
        uid: u32,
        gid: u32,
    ) -> Result<Stats> {
        self.check_background()?;

        if name.len() > MAX_NAME_LEN {
            return Err(FsError::NameTooLong.into());
        }
        let name = name.to_owned();

        let owned = self.clone();
        self.pool.execute(move |conn| {
        let _keepalive = &owned;
        let name = name.as_str();

        // BEGIN IMMEDIATE: see `mkdir` — never race the batcher's drain
        // transactions with autocommit metadata writes.
        let txn = Transaction::new_unchecked(conn, TransactionBehavior::Immediate)?;
        let result: Result<Stats> = (|| {
            // Check if already exists
            if owned.lookup_child(conn, parent_ino, name)?.is_some() {
                return Err(FsError::AlreadyExists.into());
            }

            // Create inode with mode and rdev
            let dur = SystemTime::now().duration_since(UNIX_EPOCH)?;
            let now_secs = dur.as_secs() as i64;
            let now_nsec = dur.subsec_nanos() as i64;
            let mut stmt = conn
                .prepare_cached(
                    "INSERT INTO fs_inode (mode, uid, gid, size, atime, mtime, ctime, rdev, atime_nsec, mtime_nsec, ctime_nsec)
                    VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?) RETURNING ino",
                )
                ?;
            let mut single_row_query = stmt.query((
                    mode as i64,
                    uid,
                    gid,
                    now_secs,
                    now_secs,
                    now_secs,
                    rdev as i64,
                    now_nsec,
                    now_nsec,
                    now_nsec,
                ))
                ?;
let row = single_row_query.next()?.ok_or(FsError::NotFound)?;

            let ino = Some(row.get::<_, i64>(0)?)
                .ok_or_else(|| Error::Internal("failed to get inode".to_string()))?;

            // Create directory entry
            let mut stmt = conn
                .prepare_cached("INSERT INTO fs_dentry (name, parent_ino, ino) VALUES (?, ?, ?)")
                ?;
            stmt.execute((name, parent_ino, ino))?;

            // Increment link count
            let mut stmt = conn
                .prepare_cached("UPDATE fs_inode SET nlink = nlink + 1 WHERE ino = ?")
                ?;
            stmt.execute((ino,))?;

            // Update parent directory ctime and mtime
            let mut query_statement_0 = conn.prepare_cached("UPDATE fs_inode
                     SET ctime = ?, mtime = ?, ctime_nsec = ?, mtime_nsec = ?
                     WHERE ino = ?
                     RETURNING ino")?;
let mut rows = query_statement_0.query((now_secs, now_secs, now_nsec, now_nsec, parent_ino))
                ?;
            rows.next()?.ok_or(FsError::NotFound)?;

            Ok(Stats {
                    ino,
                    mode,
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
                    rdev,
                })
        })();

        match result {
            Ok(stats) => {

                txn.commit()?;
                // Populate dentry cache only after the transaction is durable.
                owned.cache_dentry(parent_ino, name, stats.ino);
                owned.invalidate_parent_attr(parent_ino);
                owned.cache_attr(stats.clone());
                Ok(stats)
            }
            Err(error) => {
                txn.rollback()?;
                Err(error)
            }
        }
            }).await
    }

    async fn symlink(
        &self,
        parent_ino: i64,
        name: &str,
        target: &str,
        uid: u32,
        gid: u32,
    ) -> Result<Stats> {
        self.check_background()?;

        if name.len() > MAX_NAME_LEN {
            return Err(FsError::NameTooLong.into());
        }
        let name = name.to_owned();
        let target = target.to_owned();

        let owned = self.clone();
        self.pool.execute(move |conn| {
        let _keepalive = &owned;
        let name = name.as_str();
        let target = target.as_str();

        // BEGIN IMMEDIATE: see `mkdir` — never race the batcher's drain
        // transactions with autocommit metadata writes.
        let txn = Transaction::new_unchecked(conn, TransactionBehavior::Immediate)?;
        let result: Result<Stats> = (|| {
            // Check if entry already exists
            if owned.lookup_child(conn, parent_ino, name)?.is_some() {
                return Err(FsError::AlreadyExists.into());
            }

            // Create inode for symlink
            let dur = SystemTime::now().duration_since(UNIX_EPOCH)?;
            let now_secs = dur.as_secs() as i64;
            let now_nsec = dur.subsec_nanos() as i64;
            let mode = S_IFLNK | 0o777; // Symlinks typically have 777 permissions
            let size = target.len() as i64;

            let mut stmt = conn
                .prepare_cached(
                    "INSERT INTO fs_inode (mode, uid, gid, size, atime, mtime, ctime, atime_nsec, mtime_nsec, ctime_nsec)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING ino",
                )
                ?;
            let mut single_row_query = stmt.query((
                    mode, uid, gid, size, now_secs, now_secs, now_secs, now_nsec, now_nsec,
                    now_nsec,
                ))
                ?;
let row = single_row_query.next()?.ok_or(FsError::NotFound)?;

            let ino = Some(row.get::<_, i64>(0)?)
                .ok_or_else(|| Error::Internal("failed to get inode".to_string()))?;

            // Store symlink target
            conn.execute(
                "INSERT INTO fs_symlink (ino, target) VALUES (?, ?)",
                (ino, target),
            )
            ?;

            // Create directory entry
            conn.execute(
                "INSERT INTO fs_dentry (name, parent_ino, ino) VALUES (?, ?, ?)",
                (name, parent_ino, ino),
            )
            ?;

            // Increment link count
            conn.execute(
                "UPDATE fs_inode SET nlink = nlink + 1 WHERE ino = ?",
                (ino,),
            )
            ?;

            // Update parent directory ctime and mtime
            let mut query_statement_0 = conn.prepare_cached("UPDATE fs_inode
                     SET ctime = ?, mtime = ?, ctime_nsec = ?, mtime_nsec = ?
                     WHERE ino = ?
                     RETURNING ino")?;
let mut rows = query_statement_0.query((now_secs, now_secs, now_nsec, now_nsec, parent_ino))
                ?;
            rows.next()?.ok_or(FsError::NotFound)?;

            Ok(Stats {
                    ino,
                    mode,
                    nlink: 1,
                    uid,
                    gid,
                    size,
                    atime: now_secs,
                    mtime: now_secs,
                    ctime: now_secs,
                    atime_nsec: now_nsec as u32,
                    mtime_nsec: now_nsec as u32,
                    ctime_nsec: now_nsec as u32,
                    rdev: 0,
                })
        })();

        match result {
            Ok(stats) => {

                txn.commit()?;
                // Populate dentry cache only after the transaction is durable.
                owned.cache_dentry(parent_ino, name, stats.ino);
                owned.invalidate_parent_attr(parent_ino);
                owned.cache_attr(stats.clone());
                Ok(stats)
            }
            Err(error) => {
                txn.rollback()?;
                Err(error)
            }
        }
            }).await
    }

    async fn unlink(&self, parent_ino: i64, name: &str) -> Result<()> {
        self.check_background()?;

        if name.len() > MAX_NAME_LEN {
            return Err(FsError::NameTooLong.into());
        }
        self.process_deferred_reaps().await?;
        let name = name.to_owned();

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;
                let name = name.as_str();

                // BEGIN IMMEDIATE: this is the path that intermittently failed with
                // "database snapshot is stale" -> EIO when its autocommit statements
                // raced the write batcher's drain transactions (git unlinking
                // `.git/config.lock` during a clone). The transaction also makes the
                // dentry/nlink/inode removal atomic.
                let txn = Transaction::new_unchecked(conn, TransactionBehavior::Immediate)?;
                let result: Result<(i64, bool)> = (|| {
                    // Look up the child inode
                    let ino = owned
                        .lookup_child(conn, parent_ino, name)?
                        .ok_or(FsError::NotFound)?;

                    // Check if it's a directory (use rmdir for directories)
                    if let Some(mode) = store::mode(conn, ino)? {
                        if (mode & S_IFMT) == super::S_IFDIR {
                            return Err(FsError::IsADirectory.into());
                        }
                    } else {
                        return Err(FsError::NotFound.into());
                    }

                    // Delete the directory entry
                    let mut stmt = conn.prepare_cached(
                        "DELETE FROM fs_dentry WHERE parent_ino = ? AND name = ?",
                    )?;
                    stmt.execute((parent_ino, name))?;

                    // Update parent directory mtime and ctime
                    let dur = SystemTime::now().duration_since(UNIX_EPOCH)?;
                    let now_secs = dur.as_secs() as i64;
                    let now_nsec = dur.subsec_nanos() as i64;
                    let mut query_statement_0 = conn.prepare_cached(
                        "UPDATE fs_inode
                     SET mtime = ?, ctime = ?, mtime_nsec = ?, ctime_nsec = ?
                     WHERE ino = ?
                     RETURNING ino",
                    )?;
                    let mut rows = query_statement_0
                        .query((now_secs, now_secs, now_nsec, now_nsec, parent_ino))?;
                    rows.next()?.ok_or(FsError::NotFound)?;
                    drop(rows);

                    // Decrement link count and update ctime
                    let mut query_statement_1 = conn.prepare_cached(
                        "UPDATE fs_inode
                     SET nlink = nlink - 1, ctime = ?, ctime_nsec = ?
                     WHERE ino = ?
                     RETURNING ino",
                    )?;
                    let mut rows = query_statement_1.query((now_secs, now_nsec, ino))?;
                    rows.next()?.ok_or(FsError::NotFound)?;
                    drop(rows);

                    // Check if this was the last link to the inode. POSIX: while
                    // open handles exist the nlink=0 rows stay alive; the last
                    // handle drop queues the orphan for process_deferred_reaps.
                    let link_count = owned.get_link_count(conn, ino)?;
                    let removed = link_count == 0 && !owned.lifecycle.defer_reap_if_open(ino);
                    let reap_changes = if removed {
                        owned.reap_inode_with_conn(conn, ino)?
                    } else {
                        false
                    };

                    Ok((ino, reap_changes))
                })();

                match result {
                    Ok((ino, reap_changes)) => {
                        let reaped_ino = reap_changes.then_some(ino);

                        txn.commit()?;
                        if let Some(reaped_ino) = reaped_ino {
                            owned.discard_pending_for_reaped_inode(reaped_ino);
                        }
                        owned.invalidate_dentry(parent_ino, name);
                        owned.invalidate_parent_attr(parent_ino);
                        owned.invalidate_attr(ino);
                        owned.cache_negative_dentry(parent_ino, name);
                        Ok(())
                    }
                    Err(error) => {
                        txn.rollback()?;
                        Err(error)
                    }
                }
            })
            .await
    }

    async fn rmdir(&self, parent_ino: i64, name: &str) -> Result<()> {
        self.check_background()?;

        if name.len() > MAX_NAME_LEN {
            return Err(FsError::NameTooLong.into());
        }
        self.process_deferred_reaps().await?;
        let name = name.to_owned();

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;
                let name = name.as_str();

                // BEGIN IMMEDIATE: see `unlink` — never race the batcher's drain
                // transactions with autocommit metadata writes.
                let txn = Transaction::new_unchecked(conn, TransactionBehavior::Immediate)?;
                let result: Result<i64> = (|| {
                    // Look up the child inode
                    let ino = owned
                        .lookup_child(conn, parent_ino, name)?
                        .ok_or(FsError::NotFound)?;

                    if ino == ROOT_INO {
                        return Err(FsError::RootOperation.into());
                    }

                    // Check if it's a directory
                    if let Some(mode) = store::mode(conn, ino)? {
                        if (mode & S_IFMT) != super::S_IFDIR {
                            return Err(FsError::NotADirectory.into());
                        }
                    } else {
                        return Err(FsError::NotFound.into());
                    }

                    // Check if directory is empty
                    let mut stmt =
                        conn.prepare_cached("SELECT COUNT(*) FROM fs_dentry WHERE parent_ino = ?")?;
                    let mut rows = stmt.query((ino,))?;

                    if let Some(row) = rows.next()? {
                        let count = row.get::<_, i64>(0)?;
                        if count > 0 {
                            return Err(FsError::NotEmpty.into());
                        }
                    }

                    // Delete the directory entry
                    let mut stmt = conn.prepare_cached(
                        "DELETE FROM fs_dentry WHERE parent_ino = ? AND name = ?",
                    )?;
                    stmt.execute((parent_ino, name))?;

                    // Removing an empty directory drops both its parent dentry and its
                    // synthetic "." link. No live namespace path can reference the
                    // inode afterward, so retaining it at nlink=1 would create an
                    // unreachable inode that integrity checks correctly reject.
                    conn.execute("DELETE FROM fs_inode WHERE ino = ?", (ino,))?;

                    // Decrement parent nlink (removed directory's ".." link) and update timestamps
                    let dur = SystemTime::now().duration_since(UNIX_EPOCH)?;
                    let now_secs = dur.as_secs() as i64;
                    let now_nsec = dur.subsec_nanos() as i64;
                    let mut query_statement_0 = conn.prepare_cached(
                        "UPDATE fs_inode
                     SET nlink = nlink - 1, ctime = ?, mtime = ?, ctime_nsec = ?, mtime_nsec = ?
                     WHERE ino = ?
                     RETURNING ino",
                    )?;
                    let mut rows = query_statement_0
                        .query((now_secs, now_secs, now_nsec, now_nsec, parent_ino))?;
                    rows.next()?.ok_or(FsError::NotFound)?;
                    drop(rows);

                    Ok(ino)
                })();

                match result {
                    Ok(ino) => {
                        txn.commit()?;
                        owned.invalidate_dentry(parent_ino, name);
                        owned.invalidate_parent_attr(parent_ino);
                        owned.invalidate_attr(ino);
                        owned.cache_negative_dentry(parent_ino, name);
                        Ok(())
                    }
                    Err(error) => {
                        txn.rollback()?;
                        Err(error)
                    }
                }
            })
            .await
    }

    async fn link(&self, ino: i64, newparent_ino: i64, newname: &str) -> Result<Stats> {
        self.check_background()?;

        if newname.len() > MAX_NAME_LEN {
            return Err(FsError::NameTooLong.into());
        }
        let newname = newname.to_owned();

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;
                let newname = newname.as_str();

                // BEGIN IMMEDIATE: see `unlink` — never race the batcher's drain
                // transactions with autocommit metadata writes.
                let txn = Transaction::new_unchecked(conn, TransactionBehavior::Immediate)?;
                let result: Result<Stats> = (|| {
                    // Check if source inode exists and is not a directory
                    if let Some(mode) = store::mode(conn, ino)? {
                        if (mode & S_IFMT) == super::S_IFDIR {
                            return Err(FsError::IsADirectory.into());
                        }
                    } else {
                        return Err(FsError::NotFound.into());
                    }

                    // Check if destination already exists
                    if owned.lookup_child(conn, newparent_ino, newname)?.is_some() {
                        return Err(FsError::AlreadyExists.into());
                    }

                    // Create directory entry pointing to the same inode
                    conn.execute(
                        "INSERT INTO fs_dentry (name, parent_ino, ino) VALUES (?, ?, ?)",
                        (newname, newparent_ino, ino),
                    )?;

                    // Increment link count and update ctime
                    let dur = SystemTime::now().duration_since(UNIX_EPOCH)?;
                    let now_secs = dur.as_secs() as i64;
                    let now_nsec = dur.subsec_nanos() as i64;
                    let mut query_statement_0 = conn.prepare_cached(
                        "UPDATE fs_inode
                     SET nlink = nlink + 1, ctime = ?, ctime_nsec = ?
                     WHERE ino = ?
                     RETURNING ino, mode, nlink, uid, gid, size, atime, mtime, ctime, rdev,
                               atime_nsec, mtime_nsec, ctime_nsec",
                    )?;
                    let mut rows = query_statement_0.query((now_secs, now_nsec, ino))?;
                    let row = rows.next()?.ok_or(FsError::NotFound)?;
                    let stats = store::stats_from_row(row)?;

                    drop(rows);

                    // Update parent directory ctime and mtime
                    let mut query_statement_1 = conn.prepare_cached(
                        "UPDATE fs_inode
                     SET ctime = ?, mtime = ?, ctime_nsec = ?, mtime_nsec = ?
                     WHERE ino = ?
                     RETURNING ino, mode, nlink, uid, gid, size, atime, mtime, ctime, rdev,
                               atime_nsec, mtime_nsec, ctime_nsec",
                    )?;
                    let mut rows = query_statement_1.query((
                        now_secs,
                        now_secs,
                        now_nsec,
                        now_nsec,
                        newparent_ino,
                    ))?;
                    rows.next()?.ok_or(FsError::NotFound)?;

                    Ok(stats)
                })();

                match result {
                    Ok(stats) => {
                        txn.commit()?;
                        // Populate dentry cache only after the transaction is durable.
                        owned.cache_dentry(newparent_ino, newname, ino);
                        owned.invalidate_parent_attr(newparent_ino);
                        owned.invalidate_attr(ino);
                        Ok(stats)
                    }
                    Err(error) => {
                        txn.rollback()?;
                        owned.invalidate_attr(ino);
                        Err(error)
                    }
                }
            })
            .await
    }

    async fn rename(
        &self,
        oldparent_ino: i64,
        oldname: &str,
        newparent_ino: i64,
        newname: &str,
    ) -> Result<()> {
        self.check_background()?;

        self.rename_with_replaced_ino(oldparent_ino, oldname, newparent_ino, newname)
            .await
            .map(|_| ())
    }

    async fn rename_with_replaced_ino(
        &self,
        oldparent_ino: i64,
        oldname: &str,
        newparent_ino: i64,
        newname: &str,
    ) -> Result<Option<i64>> {
        self.check_background()?;

        if newname.len() > MAX_NAME_LEN {
            return Err(FsError::NameTooLong.into());
        }
        self.process_deferred_reaps().await?;
        let oldname = oldname.to_owned();
        let newname = newname.to_owned();

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;
                let oldname = oldname.as_str();
                let newname = newname.as_str();

                // Get source inode
                let src_ino = owned
                    .lookup_child(conn, oldparent_ino, oldname)?
                    .ok_or(FsError::NotFound)?;

                if src_ino == ROOT_INO {
                    return Err(FsError::RootOperation.into());
                }

                // Get source stats to check if it's a directory
                let src_stats = owned
                    .getattr_with_conn(conn, src_ino)?
                    .ok_or(FsError::NotFound)?;

                let txn = Transaction::new_unchecked(conn, TransactionBehavior::Immediate)?;

                let result: Result<(Option<i64>, bool)> = (|| {
                    let mut replaced_dst_ino = None;
                    let mut reap_changes = false;

                    if src_stats.is_directory() {
                        let mut ancestor_ino = newparent_ino;
                        let mut visited = HashSet::new();
                        while ancestor_ino != ROOT_INO {
                            if ancestor_ino == src_ino {
                                return Err(FsError::InvalidRename.into());
                            }
                            if !visited.insert(ancestor_ino) {
                                return Err(FsError::InvalidPath.into());
                            }

                            let mut stmt = conn
                                .prepare_cached("SELECT parent_ino FROM fs_dentry WHERE ino = ?")?;
                            let mut rows = stmt.query((ancestor_ino,))?;
                            let parent_ino =
                                rows.next()?.ok_or(FsError::NotFound)?.get::<_, i64>(0)?;
                            if rows.next()?.is_some() {
                                return Err(FsError::InvalidPath.into());
                            }
                            ancestor_ino = parent_ino;
                        }
                    }

                    // Check if destination exists
                    if let Some(dst_ino) = owned.lookup_child(conn, newparent_ino, newname)? {
                        replaced_dst_ino = Some(dst_ino);
                        let dst_stats = owned
                            .getattr_with_conn(conn, dst_ino)?
                            .ok_or(FsError::NotFound)?;

                        // Can't replace directory with non-directory
                        if dst_stats.is_directory() && !src_stats.is_directory() {
                            return Err(FsError::IsADirectory.into());
                        }

                        // Can't replace non-directory with directory
                        if !dst_stats.is_directory() && src_stats.is_directory() {
                            return Err(FsError::NotADirectory.into());
                        }

                        // If destination is directory, it must be empty
                        if dst_stats.is_directory() {
                            let mut stmt = conn.prepare_cached(
                                "SELECT COUNT(*) FROM fs_dentry WHERE parent_ino = ?",
                            )?;
                            let mut rows = stmt.query((dst_ino,))?;

                            if let Some(row) = rows.next()? {
                                let count = row.get::<_, i64>(0)?;
                                if count > 0 {
                                    return Err(FsError::NotEmpty.into());
                                }
                            }
                        }

                        // Remove destination entry
                        let mut stmt = conn.prepare_cached(
                            "DELETE FROM fs_dentry WHERE parent_ino = ? AND name = ?",
                        )?;
                        stmt.execute((newparent_ino, newname))?;

                        // Decrement link count and update ctime on destination inode
                        let dur_dec = SystemTime::now()
                            .duration_since(UNIX_EPOCH)
                            .unwrap_or_default();
                        let now_dec = dur_dec.as_secs() as i64;
                        let now_dec_nsec = dur_dec.subsec_nanos() as i64;
                        let mut query_statement_0 = conn.prepare_cached(
                            "UPDATE fs_inode
                         SET nlink = nlink - 1, ctime = ?, ctime_nsec = ?
                         WHERE ino = ?
                         RETURNING ino",
                        )?;
                        let mut rows = query_statement_0.query((now_dec, now_dec_nsec, dst_ino))?;
                        rows.next()?.ok_or(FsError::NotFound)?;
                        drop(rows);

                        // Clean up destination inode if no more links (deferred while
                        // open handles exist — see lifecycle).
                        let link_count = owned.get_link_count(conn, dst_ino)?;
                        if link_count == 0 && !owned.lifecycle.defer_reap_if_open(dst_ino) {
                            reap_changes = owned.reap_inode_with_conn(conn, dst_ino)?;
                        }
                    }

                    // Update the dentry: change parent and/or name
                    let mut stmt = conn.prepare_cached(
                        "UPDATE fs_dentry
                     SET parent_ino = ?, name = ?
                     WHERE parent_ino = ? AND name = ?
                     RETURNING id",
                    )?;
                    let _: i64 = stmt
                        .query_row((newparent_ino, newname, oldparent_ino, oldname), |row| {
                            row.get(0)
                        })?;

                    // If renaming a directory across parents, adjust parent nlink counts
                    // (the ".." link moves from old parent to new parent)
                    if src_stats.is_directory() && oldparent_ino != newparent_ino {
                        let mut stmt = conn.prepare_cached(
                            "UPDATE fs_inode SET nlink = nlink - 1 WHERE ino = ?",
                        )?;
                        stmt.execute((oldparent_ino,))?;

                        let mut stmt = conn.prepare_cached(
                            "UPDATE fs_inode SET nlink = nlink + 1 WHERE ino = ?",
                        )?;
                        stmt.execute((newparent_ino,))?;
                    }

                    // Update ctime of the inode
                    let dur = SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .unwrap_or_default();
                    let now_secs = dur.as_secs() as i64;
                    let now_nsec = dur.subsec_nanos() as i64;

                    let mut query_statement_1 = conn.prepare_cached(
                        "UPDATE fs_inode SET ctime = ?, ctime_nsec = ? WHERE ino = ?
                     RETURNING ino",
                    )?;
                    let mut rows = query_statement_1.query((now_secs, now_nsec, src_ino))?;
                    rows.next()?.ok_or(FsError::NotFound)?;
                    drop(rows);

                    // Update source parent directory timestamps
                    let mut query_statement_2 = conn.prepare_cached(
                        "UPDATE fs_inode
                     SET mtime = ?, ctime = ?, mtime_nsec = ?, ctime_nsec = ?
                     WHERE ino = ?
                     RETURNING ino",
                    )?;
                    let mut rows = query_statement_2.query((
                        now_secs,
                        now_secs,
                        now_nsec,
                        now_nsec,
                        oldparent_ino,
                    ))?;
                    rows.next()?.ok_or(FsError::NotFound)?;
                    drop(rows);

                    // Update destination parent directory timestamps
                    if newparent_ino != oldparent_ino {
                        let mut query_statement_3 = conn.prepare_cached(
                            "UPDATE fs_inode
                         SET mtime = ?, ctime = ?, mtime_nsec = ?, ctime_nsec = ?
                         WHERE ino = ?
                         RETURNING ino",
                        )?;
                        let mut rows = query_statement_3.query((
                            now_secs,
                            now_secs,
                            now_nsec,
                            now_nsec,
                            newparent_ino,
                        ))?;
                        rows.next()?.ok_or(FsError::NotFound)?;
                    }

                    Ok((replaced_dst_ino, reap_changes))
                })();

                match result {
                    Ok((replaced_dst_ino, reap_changes)) => {
                        let reaped_dst_ino = if reap_changes { replaced_dst_ino } else { None };

                        txn.commit()?;
                        if let Some(reaped_dst_ino) = reaped_dst_ino {
                            owned.discard_pending_for_reaped_inode(reaped_dst_ino);
                        }

                        // Invalidate cache for source and destination
                        owned.invalidate_dentry(oldparent_ino, oldname);
                        owned.invalidate_dentry(newparent_ino, newname);
                        owned.invalidate_attr(src_ino);
                        owned.invalidate_parent_attr(oldparent_ino);
                        owned.invalidate_parent_attr(newparent_ino);
                        if let Some(dst_ino) = replaced_dst_ino {
                            owned.invalidate_attr(dst_ino);
                        }

                        // Add exact post-rename namespace state to the caches.
                        if oldparent_ino != newparent_ino || oldname != newname {
                            owned.cache_negative_dentry(oldparent_ino, oldname);
                        }
                        owned.cache_dentry(newparent_ino, newname, src_ino);

                        Ok(replaced_dst_ino)
                    }
                    Err(e) => {
                        txn.rollback()?;
                        Err(e)
                    }
                }
            })
            .await
    }

    async fn statfs(&self) -> Result<FilesystemStats> {
        self.check_background()?;

        Vfs::statfs(self).await
    }

    async fn drain_inode_writes(&self, ino: i64) -> Result<()> {
        self.check_background()?;

        Vfs::drain_inode_writes(self, ino).await
    }

    async fn drain_all(&self) -> Result<()> {
        self.check_background()?;

        Vfs::drain_all(self).await
    }

    async fn finalize(&self) -> Result<()> {
        self.check_background()?;

        Vfs::finalize(self).await
    }

    fn register_reap_hook(&self, hook: Arc<dyn ReapHook>) -> bool {
        Vfs::register_reap_hook(self, hook)
    }

    // `forget` deliberately uses the default no-op trait impl: a FORGET only
    // drops the kernel's reference to the inode. Pending batched writes stay
    // readable through the Tier-4 overlay and are committed by the batcher
    // timer/bytes triggers, fsync, or finalize — committing them here issued
    // one serial SQLite transaction per written file during clone workloads
    // (the kernel FORGETs each file shortly after our post-write entry
    // invalidation).
}
