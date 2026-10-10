use super::super::vfs::store::{self, ChunkWriteHooks, WriteRangeRef};
use super::*;
use crate::fs::{File, FsError, WriteRange};
use std::sync::Arc;
use tokio_rusqlite::rusqlite::{Transaction, TransactionBehavior};

#[derive(Debug, Clone)]
pub(super) struct PartialOrigin {
    pub(super) base_identity: String,
    pub(super) base_path: String,
    pub(super) base_fingerprint_size: i64,
    pub(super) base_mtime: i64,
    pub(super) base_mtime_nsec: u32,
    pub(super) base_ctime: i64,
    pub(super) base_ctime_nsec: u32,
}

impl PartialOrigin {
    pub(super) fn fingerprint(&self) -> crate::fs::base_fingerprint::BaseFingerprint {
        crate::fs::base_fingerprint::BaseFingerprint {
            size: self.base_fingerprint_size,
            mtime: self.base_mtime,
            mtime_nsec: self.base_mtime_nsec as i64,
            ctime: self.base_ctime,
            ctime_nsec: self.base_ctime_nsec as i64,
        }
    }
}

#[derive(Clone)]
pub(super) struct OverlayPartialFile {
    pub(super) runtime: tokio::runtime::Handle,
    pub(super) delta: Vfs,
    // Retain the inode for the lifetime of this file, like ordinary delta opens.
    pub(super) _delta_handle: super::super::BoxedFile,
    pub(super) base: Arc<dyn FileSystem>,
    pub(super) base_file: super::super::BoxedFile,
    pub(super) base_validator: Option<Arc<dyn BaseValidator>>,
    pub(super) origin: PartialOrigin,
    pub(super) overlay_ino: i64,
    pub(super) delta_ino: i64,
    pub(super) chunk_size: usize,
}

struct PartialOriginChunkHooks<'a> {
    file: &'a OverlayPartialFile,
}

impl ChunkWriteHooks for PartialOriginChunkHooks<'_> {
    fn seed_missing_chunk(
        &self,
        conn: &Connection,
        ino: i64,
        geometry: crate::config::Geometry,
        chunk_index: i64,
    ) -> Result<Option<Vec<u8>>> {
        debug_assert_eq!(ino, self.file.delta_ino);
        let chunk_index = u64::try_from(chunk_index)
            .map_err(|_| Error::Internal("negative chunk index".to_string()))?;
        let base_size = self.file.partial_base_size_with_conn(conn)?;
        let chunk_start = chunk_index
            .checked_mul(geometry.chunk_size as u64)
            .ok_or_else(|| Error::Internal("chunk offset overflow".to_string()))?;
        if chunk_start >= base_size {
            return Ok(None);
        }

        self.file
            .runtime
            .block_on(self.file.validate_current_origin())?;
        let readable = std::cmp::min(geometry.chunk_size as u64, base_size - chunk_start);
        let mut chunk = self
            .file
            .runtime
            .block_on(self.file.base_file.pread(chunk_start, readable))?;
        chunk.resize(geometry.chunk_size, 0);
        Ok(Some(chunk))
    }

    fn chunk_written(&self, conn: &Connection, ino: i64, chunk_index: i64) -> Result<()> {
        debug_assert_eq!(ino, self.file.delta_ino);
        conn.execute(
            "INSERT OR IGNORE INTO fs_chunk_override (delta_ino, chunk_index) VALUES (?, ?)",
            (ino, chunk_index),
        )?;
        Ok(())
    }
}

impl OverlayFS {
    pub(super) async fn partial_origin_for_delta(
        &self,
        delta_ino: i64,
    ) -> Result<Option<PartialOrigin>> {
        self.delta.get_pool().check_ready()?;

        let owned = self.clone();
        self.delta.get_pool().execute(move |conn| {
        let _keepalive = &owned;

        let mut query_statement_0 = conn.prepare_cached("SELECT base_ino, base_path, base_size, base_fingerprint_size,
                        base_mtime, base_mtime_nsec, base_ctime, base_ctime_nsec, created_at,
                        (SELECT base_identity FROM fs_origin WHERE fs_origin.delta_ino = fs_partial_origin.delta_ino)
                 FROM fs_partial_origin WHERE delta_ino = ?")?;
let mut rows = query_statement_0.query((delta_ino,))
            ?;
        if let Some(row) = rows.next()? {
            let base_fingerprint_size: i64 = row.get(3)?;
            if base_fingerprint_size < 0 {
                return Err(
                    FsError::Corrupt("partial origin has no base fingerprint".into()).into(),
                );
            }
            Ok(Some(PartialOrigin {
                base_identity: row.get(9)?,
                base_path: row.get(1)?,
                base_fingerprint_size,
                base_mtime: row.get(4)?,
                base_mtime_nsec: row.get(5)?,
                base_ctime: row.get(6)?,
                base_ctime_nsec: row.get(7)?,
            }))
        } else {
            Ok(None)
        }
            }).await
    }

    pub(super) fn add_partial_origin_mapping_with_conn(
        conn: &Connection,
        delta_ino: i64,
        base_ino: i64,
        base_path: &str,
        base_stats: &Stats,
        now: i64,
    ) -> Result<()> {
        conn.execute(
            "INSERT OR REPLACE INTO fs_partial_origin (
                delta_ino, base_ino, base_path, base_size, created_at
             ) VALUES (?1, ?2, ?3, ?4, ?5)",
            (delta_ino, base_ino, base_path, base_stats.size, now),
        )?;
        conn.execute(
            "UPDATE fs_partial_origin
             SET base_fingerprint_size = ?1, base_mtime = ?2, base_mtime_nsec = ?3
             WHERE delta_ino = ?4",
            (
                base_stats.size,
                base_stats.mtime,
                base_stats.mtime_nsec as i64,
                delta_ino,
            ),
        )?;
        conn.execute(
            "UPDATE fs_partial_origin
             SET base_ctime = ?1, base_ctime_nsec = ?2
             WHERE delta_ino = ?3",
            (base_stats.ctime, base_stats.ctime_nsec as i64, delta_ino),
        )?;
        Ok(())
    }
}

#[async_trait]
impl File for OverlayPartialFile {
    async fn pread(&self, offset: u64, size: u64) -> Result<Vec<u8>> {
        self.delta.get_pool().check_ready()?;

        self.validate_current_origin().await?;

        let owned = self.clone();
        self.delta
            .get_pool()
            .execute(move |conn| {
                let _keepalive = &owned;
                let snapshot = conn.unchecked_transaction()?;
                let result = (|| {
                    let file_size = owned.delta_file_size_with_conn(conn)?;
                    if offset >= file_size || size == 0 {
                        return Ok(Vec::new());
                    }

                    let read_len = std::cmp::min(size, file_size - offset) as usize;
                    let chunk_size = owned.chunk_size as u64;
                    let mut result = Vec::with_capacity(read_len);

                    while result.len() < read_len {
                        let current_offset = offset + result.len() as u64;
                        let chunk_index = current_offset / chunk_size;
                        let offset_in_chunk = (current_offset % chunk_size) as usize;
                        let take = std::cmp::min(
                            owned.chunk_size - offset_in_chunk,
                            read_len.saturating_sub(result.len()),
                        );

                        let chunk = owned.read_merged_chunk_with_conn(conn, chunk_index)?;
                        result.extend_from_slice(&chunk[offset_in_chunk..offset_in_chunk + take]);
                    }

                    Ok(result)
                })();
                if result.is_ok() {
                    snapshot.commit()?;
                }
                result
            })
            .await
    }

    async fn pwrite(&self, offset: u64, data: &[u8]) -> Result<()> {
        if data.is_empty() {
            return Ok(());
        }
        self.pwrite_ranges(vec![WriteRange {
            offset,
            data: data.to_vec(),
        }])
        .await
    }

    async fn pwrite_ranges(&self, ranges: Vec<WriteRange>) -> Result<()> {
        self.delta.get_pool().check_ready()?;

        if ranges.iter().all(|range| range.data.is_empty()) {
            return Ok(());
        }

        let owned = self.clone();
        self.delta
            .get_pool()
            .execute(move |conn| {
                let _keepalive = &owned;

                let txn = Transaction::new_unchecked(conn, TransactionBehavior::Immediate)?;
                let range_refs: Vec<_> = ranges
                    .iter()
                    .map(|range| WriteRangeRef {
                        offset: range.offset,
                        data: range.data.as_slice(),
                    })
                    .collect();
                let hooks = PartialOriginChunkHooks { file: &owned };

                let result = store::write_ranges_with_chunk_hooks(
                    conn,
                    owned.delta_ino,
                    owned.geometry(),
                    &range_refs,
                    &hooks,
                );

                match result {
                    Ok(()) => {
                        txn.commit()?;
                        owned.delta.invalidate_attr(owned.delta_ino);
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

    async fn truncate(&self, size: u64) -> Result<()> {
        self.delta.get_pool().check_ready()?;

        let owned = self.clone();
        self.delta
            .get_pool()
            .execute(move |conn| {
                let _keepalive = &owned;

                let txn = Transaction::new_unchecked(conn, TransactionBehavior::Immediate)?;
                let hooks = PartialOriginChunkHooks { file: &owned };

                let result = (|| {
                    store::truncate_with_chunk_hooks(
                        conn,
                        owned.delta_ino,
                        owned.geometry(),
                        size,
                        &hooks,
                    )?;
                    owned.prune_chunk_overrides_after_truncate(conn, size)?;

                    let origin_base_size = owned.partial_base_size_with_conn(conn)?;
                    if size < origin_base_size {
                        conn.execute(
                            "UPDATE fs_partial_origin SET base_size = ? WHERE delta_ino = ?",
                            (size as i64, owned.delta_ino),
                        )?;
                    }
                    Ok::<_, Error>(())
                })();

                match result {
                    Ok(()) => {
                        txn.commit()?;
                        owned.delta.invalidate_attr(owned.delta_ino);
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
        self.delta.fsync().await
    }

    async fn fstat(&self) -> Result<Stats> {
        let mut stats = FileSystem::getattr(&self.delta, self.delta_ino)
            .await?
            .ok_or(FsError::NotFound)?;
        stats.ino = self.overlay_ino;
        Ok(stats)
    }
}

impl OverlayPartialFile {
    fn geometry(&self) -> crate::config::Geometry {
        crate::config::Geometry {
            chunk_size: self.chunk_size,
            inline_threshold: self.delta.inline_threshold(),
        }
    }

    async fn resolve_origin_base_stats(&self) -> Result<Option<Stats>> {
        let mut ino = ROOT_INO;
        if self.origin.base_path == "/" {
            return self.base.getattr(ino).await;
        }

        let mut stats = None;
        for component in self.origin.base_path.split('/').filter(|s| !s.is_empty()) {
            let Some(next) = self.base.lookup(ino, component).await? else {
                return Ok(None);
            };
            ino = next.ino;
            stats = Some(next);
        }
        Ok(stats)
    }

    async fn validate_current_origin(&self) -> Result<()> {
        let stats = self
            .resolve_origin_base_stats()
            .await?
            .ok_or(FsError::NotFound)?;
        if self.base.file_identity(stats.ino)? != self.origin.base_identity {
            return Err(FsError::Corrupt("partial-origin base identity changed".into()).into());
        }
        if let Some(validator) = &self.base_validator {
            return validator.validate(self.base.as_ref(), &stats);
        }
        if crate::fs::base_fingerprint::BaseFingerprint::from_stats(&stats)
            != self.origin.fingerprint()
        {
            return Err(Error::Internal(format!(
                "partial-origin base changed for {}",
                self.origin.base_path
            )));
        }
        Ok(())
    }

    fn delta_file_size_with_conn(&self, conn: &Connection) -> Result<u64> {
        let size: i64 = conn.query_row(
            "SELECT size FROM fs_inode WHERE ino = ?",
            [self.delta_ino],
            |row| row.get(0),
        )?;
        u64::try_from(size).map_err(|_| FsError::Corrupt("negative size".into()).into())
    }

    fn prune_chunk_overrides_after_truncate(&self, conn: &Connection, size: u64) -> Result<()> {
        let last = if size == 0 {
            -1
        } else {
            ((size - 1) / self.chunk_size as u64) as i64
        };
        conn.execute(
            "DELETE FROM fs_chunk_override WHERE delta_ino = ? AND chunk_index > ?",
            (self.delta_ino, last),
        )?;
        Ok(())
    }

    fn partial_base_size_with_conn(&self, conn: &Connection) -> Result<u64> {
        let size: i64 = conn.query_row(
            "SELECT base_size FROM fs_partial_origin WHERE delta_ino = ?",
            [self.delta_ino],
            |row| row.get(0),
        )?;
        u64::try_from(size).map_err(|_| FsError::Corrupt("negative base_size".into()).into())
    }

    fn chunk_is_override_with_conn(&self, conn: &Connection, chunk_index: u64) -> Result<bool> {
        let mut query_statement_0 = conn.prepare_cached(
            "SELECT 1 FROM fs_chunk_override WHERE delta_ino = ? AND chunk_index = ?",
        )?;
        let mut rows = query_statement_0.query((self.delta_ino, chunk_index as i64))?;
        Ok(rows.next()?.is_some())
    }

    fn read_merged_chunk_with_conn(&self, conn: &Connection, chunk_index: u64) -> Result<Vec<u8>> {
        if self.chunk_is_override_with_conn(conn, chunk_index)? {
            let chunk_start = chunk_index
                .checked_mul(self.chunk_size as u64)
                .ok_or_else(|| Error::Internal("chunk offset overflow".to_string()))?;
            let mut chunk = store::read(
                conn,
                self.delta_ino,
                self.geometry(),
                chunk_start,
                self.chunk_size as u64,
            )?;
            chunk.resize(self.chunk_size, 0);
            return Ok(chunk);
        }

        let base_size = self.partial_base_size_with_conn(conn)?;
        let chunk_start = chunk_index
            .checked_mul(self.chunk_size as u64)
            .ok_or_else(|| Error::Internal("chunk offset overflow".to_string()))?;
        let mut chunk = if chunk_start < base_size {
            self.runtime.block_on(self.validate_current_origin())?;
            let readable = std::cmp::min(self.chunk_size as u64, base_size - chunk_start);
            self.runtime
                .block_on(self.base_file.pread(chunk_start, readable))?
        } else {
            Vec::new()
        };
        chunk.resize(self.chunk_size, 0);
        Ok(chunk)
    }
}
