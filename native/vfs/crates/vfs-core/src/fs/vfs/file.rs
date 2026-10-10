//! Open-file handle implementation for Vfs.
//!
//! `VfsFile` owns per-handle read, write, truncate, fsync, and fstat
//! behavior. It overlays pending batched writes onto committed SQLite state
//! without forcing drains on read paths, preserving the noopen/writeback hot
//! path.

use crate::fs::{File, FsError, Stats, WriteRange};
use async_trait::async_trait;

use super::batcher::EnqueueOutcome;
use super::store::WriteRangeRef;
use super::*;

/// An open file handle for Vfs.
///
/// This struct holds the inode number resolved at open time, allowing
/// efficient read/write/fsync operations without path lookups.
#[derive(Clone)]
pub struct VfsFile {
    pub(super) pool: ConnectionPool,
    pub(super) ino: i64,
    pub(super) chunk_size: usize,
    pub(super) inline_threshold: usize,
    pub(super) attr_cache: Arc<AttrCache>,
    pub(super) pending_view: Option<BatcherPendingView>,
    pub(super) write_drain: Option<BatcherDrain>,
    /// Same semantics as the field on `Vfs`; cloned at open time so the
    /// hot read/write path doesn't have to chase an extra indirection.
    pub(super) overlay_reads: bool,
    pub(super) journal: JournalCtx,
    /// Present for user-visible handles so unlink defers inode reaping while
    /// they live. This remains optional until lifecycle extraction flattens
    /// the handle construction API.
    pub(super) _open_guard: Option<Arc<OpenInodeGuard>>,
}

#[async_trait]
impl File for VfsFile {
    async fn pread(&self, offset: u64, size: u64) -> Result<Vec<u8>> {
        self.check_failure()?;
        if size == 0 {
            return Ok(Vec::new());
        }
        if !self.overlay_reads {
            self.drain_writes().await?;
        }
        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                loop {
                    let view = owned.pending_view.as_ref().filter(|_| owned.overlay_reads);
                    let generation = view.map(|view| view.pending_generation(owned.ino));
                    let pending_max = view.and_then(|view| view.pending_max_end(owned.ino));
                    // One SQLite snapshot covers both inode layout and chunks.
                    let txn = conn.transaction()?;
                    let metadata = store::file_storage(&txn, owned.ino)?;
                    let effective_size =
                        pending_max.map_or(metadata.size(), |end| metadata.size().max(end));
                    let read_size = size.min(effective_size.saturating_sub(offset));
                    let base_window = metadata.size().saturating_sub(offset).min(read_size);
                    let mut result = if base_window > 0 {
                        store::read_from_storage(
                            &txn,
                            owned.ino,
                            owned.geometry(),
                            &metadata,
                            offset,
                            base_window,
                        )?
                    } else {
                        Vec::new()
                    };
                    result.resize(read_size as usize, 0);
                    if let Some(view) = view {
                        view.overlay_read(owned.ino, offset, &mut result)?;
                    }
                    let changed = view
                        .zip(generation)
                        .is_some_and(|(view, before)| view.pending_generation(owned.ino) != before);
                    txn.commit()?;
                    // Drain cleanup advances this existing generation counter.
                    // A changed overlay invalidates the combined SQL/pending view.
                    if changed {
                        continue;
                    }
                    return Ok(result);
                }
            })
            .await
    }

    async fn pwrite(&self, offset: u64, data: &[u8]) -> Result<()> {
        self.check_failure()?;

        if data.is_empty() {
            return Ok(());
        }
        // Tier Four: with the batcher wired AND overlay reads enabled,
        // route through enqueue so the overlay holds the write and readers
        // see it via `pread`'s peek_pending merge. Drain only on
        // fsync/destroy/timer. When `VFS_OVERLAY_READS=0` the
        // overlay-reads escape hatch is engaged: skip the batcher and commit
        // directly so the legacy Tier 3 read path (which drains before
        // reading) sees the write.
        if let Some(drain) = &self.write_drain {
            if self.overlay_reads {
                let outcome = drain.enqueue(
                    self.ino,
                    vec![WriteRange {
                        offset,
                        data: data.to_vec(),
                    }],
                )?;
                return Self::finish_enqueue(drain, self.ino, outcome).await;
            }
        }
        // Fallback (no batcher): direct commit. drain_writes is a no-op
        // when there's no batcher, but keeping the call here makes the
        // contract explicit.
        self.drain_writes().await?;
        let data = data.to_vec();

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let mut txn = MutationTxn::begin(conn, owned.journal.clone())?;
                let ranges = [WriteRangeRef {
                    offset,
                    data: &data,
                }];
                let result = store::write_ranges(
                    txn.conn(),
                    owned.ino,
                    owned.geometry(),
                    &ranges,
                    false,
                    None,
                );
                match result {
                    Ok(changes) => {
                        txn.record_storage_changes("write", changes)?;
                        txn.commit()?;
                        owned.attr_cache.remove(owned.ino);
                        Ok(())
                    }
                    Err(e) => {
                        txn.rollback()?;
                        Err(e)
                    }
                }
            })
            .await
    }

    async fn pwrite_ranges(&self, ranges: Vec<WriteRange>) -> Result<()> {
        self.check_failure()?;

        if ranges.iter().all(|range| range.data.is_empty()) {
            return Ok(());
        }
        // Tier Four: route through the batcher when overlay reads are
        // enabled; otherwise commit immediately (escape hatch — see pwrite).
        if let Some(drain) = &self.write_drain {
            if self.overlay_reads {
                let outcome = drain.enqueue(self.ino, ranges)?;
                return Self::finish_enqueue(drain, self.ino, outcome).await;
            }
        }
        self.drain_writes().await?;

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let mut txn = MutationTxn::begin(conn, owned.journal.clone())?;
                let range_refs: Vec<_> = ranges
                    .iter()
                    .map(|range| WriteRangeRef {
                        offset: range.offset,
                        data: range.data.as_slice(),
                    })
                    .collect();
                let result = store::write_ranges(
                    txn.conn(),
                    owned.ino,
                    owned.geometry(),
                    &range_refs,
                    false,
                    None,
                );
                match result {
                    Ok(changes) => {
                        txn.record_storage_changes("write", changes)?;
                        txn.commit()?;
                        owned.attr_cache.remove(owned.ino);
                        Ok(())
                    }
                    Err(e) => {
                        txn.rollback()?;
                        Err(e)
                    }
                }
            })
            .await
    }

    async fn pwrite_ranges_batched(&self, ranges: Vec<WriteRange>) -> Result<()> {
        self.check_failure()?;

        if ranges.iter().all(|range| range.data.is_empty()) {
            return Ok(());
        }

        if let Some(drain) = &self.write_drain {
            let outcome = drain.enqueue(self.ino, ranges)?;
            Self::finish_enqueue(drain, self.ino, outcome).await
        } else {
            self.pwrite_ranges(ranges).await
        }
    }

    async fn truncate(&self, new_size: u64) -> Result<()> {
        self.check_failure()?;

        // Tier Four: shrink the in-memory overlay BEFORE touching SQLite, so
        // a concurrent reader doesn't observe pending bytes past the new EOF
        // between the SQLite truncate and the batcher catching up.
        if let Some(drain) = &self.write_drain {
            drain.truncate_pending(self.ino, new_size);
        }
        // Drain remaining pending so the SQLite truncate sees a consistent
        // size. With truncate_pending called above, the only pending left is
        // for offsets < new_size, which will be applied by the timer / next
        // drain trigger. We still drain here so the SQLite size after this
        // call exactly matches `new_size`.
        self.drain_writes().await?;

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let mut txn = MutationTxn::begin(conn, owned.journal.clone())?;
                let result = store::truncate(txn.conn(), owned.ino, owned.geometry(), new_size);
                match result {
                    Ok(changes) => {
                        txn.record_storage_changes("truncate", changes)?;
                        txn.commit()?;
                        owned.attr_cache.remove(owned.ino);
                        Ok(())
                    }
                    Err(e) => {
                        txn.rollback()?;
                        Err(e)
                    }
                }
            })
            .await
    }

    async fn fsync(&self) -> Result<()> {
        self.check_failure()?;

        // Tier Four: fsync remains the explicit durability barrier — drain the
        // batcher so the WAL checkpoint that follows captures every pending
        // write.
        self.drain_writes().await?;

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _owned = &owned;

                conn.prepare_cached(DURABLE_SYNCHRONOUS_SQL)?.execute([])?;
                owned.pool.checkpoint(conn)?;
                conn.prepare_cached(BASELINE_SYNCHRONOUS_SQL)?.execute([])?;
                Ok(())
            })
            .await
    }

    async fn fstat(&self) -> Result<Stats> {
        self.check_failure()?;

        self.drain_writes().await?;
        if let Some(stats) = self.attr_cache.get(self.ino) {
            return Ok(stats);
        }

        let owned = self.clone();
        self.pool.execute(move |conn| {

        let generation = owned
            .pending_view
            .as_ref()
            .map(|view| view.pending_generation(owned.ino));
        let mut stmt = conn
            .prepare_cached("SELECT ino, mode, nlink, uid, gid, size, atime, mtime, ctime, rdev, atime_nsec, mtime_nsec, ctime_nsec FROM fs_inode WHERE ino = ?")
            ?;
        let mut rows = stmt.query((owned.ino,))?;

        if let Some(row) = rows.next()? {
            let stats = store::stats_from_row(row)?;
            if let (Some(view), Some(generation)) = (&owned.pending_view, generation) {
                if view.pending_generation(stats.ino) == generation {
                    owned.attr_cache.insert(stats.clone());
                }
            } else {
                owned.attr_cache.insert(stats.clone());
            }
            Ok(stats)
        } else {
            Err(FsError::NotFound.into())
        }
            }).await
    }

    async fn drain_writes(&self) -> Result<()> {
        self.check_failure()?;

        if let Some(drain) = &self.write_drain {
            drain.drain_inode(self.ino).await?;
        }
        Ok(())
    }
}

impl VfsFile {
    fn check_failure(&self) -> Result<()> {
        self.pool.check_ready()?;
        if let Some(drain) = &self.write_drain {
            drain.check_completed()?;
        }
        Ok(())
    }

    async fn finish_enqueue(drain: &BatcherDrain, ino: i64, outcome: EnqueueOutcome) -> Result<()> {
        if outcome.drain_all {
            drain.drain_all_bytes().await
        } else if outcome.drain_inode {
            drain.drain_inode_bytes(ino).await
        } else {
            Ok(())
        }
    }

    fn geometry(&self) -> Geometry {
        Geometry {
            chunk_size: self.chunk_size,
            inline_threshold: self.inline_threshold,
        }
    }
}
