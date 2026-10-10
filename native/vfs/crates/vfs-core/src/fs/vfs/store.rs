use std::collections::BTreeMap;

use tokio_rusqlite::rusqlite::{types::Value, Connection};

use crate::config::Geometry;
use crate::error::{Error, Result};
use crate::fs::{FsError, Stats};

use super::batcher::{write_commit_time_sets, PendingTimeChange};
use super::{current_timestamp, InodeRow, STORAGE_CHUNKED, STORAGE_INLINE};

pub(super) struct FileStorage {
    pub(super) inode: InodeRow,
}

impl FileStorage {
    pub(super) fn size(&self) -> u64 {
        self.inode.size as u64
    }

    fn storage_kind(&self) -> i64 {
        self.inode.storage_kind
    }

    fn inline_data(&self) -> Option<Vec<u8>> {
        self.inode.data_inline.clone()
    }
}

#[derive(Clone, Debug)]
pub(crate) enum DataDelta {
    Upsert {
        ino: i64,
        chunk_index: i64,
        digest: Vec<u8>,
    },
    Delete {
        ino: i64,
        chunk_index: i64,
    },
}

#[derive(Clone, Debug)]
pub(crate) struct StorageChanges {
    pub(crate) inode: InodeRow,
    pub(crate) data: Vec<DataDelta>,
}

pub(in crate::fs) struct WriteRangeRef<'a> {
    pub(in crate::fs) offset: u64,
    pub(in crate::fs) data: &'a [u8],
}

pub(in crate::fs) trait ChunkWriteHooks: Send + Sync {
    fn seed_missing_chunk(
        &self,
        conn: &Connection,
        ino: i64,
        geometry: Geometry,
        chunk_index: i64,
    ) -> Result<Option<Vec<u8>>>;

    fn chunk_written(&self, conn: &Connection, ino: i64, chunk_index: i64) -> Result<()>;
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(in crate::fs) struct NormalizedWriteRange {
    pub(in crate::fs) offset: u64,
    pub(in crate::fs) data: Vec<u8>,
}

impl NormalizedWriteRange {
    fn end(&self) -> u64 {
        self.offset + self.data.len() as u64
    }
}

pub(in crate::fs) fn normalize_write_ranges(
    ranges: &[WriteRangeRef<'_>],
) -> Result<Vec<NormalizedWriteRange>> {
    let mut merged_ranges: BTreeMap<u64, Vec<u8>> = BTreeMap::new();

    for range in ranges {
        if range.data.is_empty() {
            continue;
        }

        let data_len = u64::try_from(range.data.len())
            .map_err(|_| Error::Internal("file write length overflow".to_string()))?;
        let write_start = range.offset;
        let write_end = write_start
            .checked_add(data_len)
            .ok_or_else(|| Error::Internal("file write offset overflow".to_string()))?;
        let mut start = write_start;
        let mut end = write_end;
        let mut existing_ranges = Vec::new();

        if let Some((&prev_start, prev_data)) = merged_ranges.range(..=write_start).next_back() {
            let prev_end = prev_start
                .checked_add(prev_data.len() as u64)
                .ok_or_else(|| Error::Internal("file write offset overflow".to_string()))?;

            if prev_end >= write_start {
                let prev_data = prev_data.clone();
                merged_ranges.remove(&prev_start);

                start = prev_start;
                end = end.max(prev_end);
                existing_ranges.push((prev_start, prev_data));
            }
        }

        loop {
            let next = merged_ranges
                .range(start..)
                .next()
                .map(|(&next_start, next_data)| (next_start, next_data.clone()));

            let Some((next_start, next_data)) = next else {
                break;
            };

            if next_start > end {
                break;
            }

            let next_end = next_start
                .checked_add(next_data.len() as u64)
                .ok_or_else(|| Error::Internal("file write offset overflow".to_string()))?;
            merged_ranges.remove(&next_start);

            end = end.max(next_end);
            existing_ranges.push((next_start, next_data));
        }

        let merged_len = usize::try_from(end - start)
            .map_err(|_| Error::Internal("file write range too large".to_string()))?;
        let mut merged = vec![0; merged_len];
        for (range_start, range_data) in existing_ranges {
            let range_offset = usize::try_from(range_start - start)
                .map_err(|_| Error::Internal("file write range too large".to_string()))?;
            merged[range_offset..range_offset + range_data.len()].copy_from_slice(&range_data);
        }

        let write_offset = usize::try_from(write_start - start)
            .map_err(|_| Error::Internal("file write range too large".to_string()))?;
        merged[write_offset..write_offset + range.data.len()].copy_from_slice(range.data);

        merged_ranges.insert(start, merged);
    }

    Ok(merged_ranges
        .into_iter()
        .map(|(offset, data)| NormalizedWriteRange { offset, data })
        .collect())
}

pub(super) fn dense_after_inline_write_batch(
    current_size: u64,
    new_size: u64,
    ranges: &[NormalizedWriteRange],
) -> bool {
    let mut covered_end = current_size;

    for range in ranges {
        let range_end = range.end();
        if range_end <= covered_end {
            continue;
        }
        if range.offset > covered_end {
            return false;
        }
        covered_end = range_end;
        if covered_end >= new_size {
            return true;
        }
    }

    covered_end >= new_size
}

pub(super) fn file_storage(conn: &Connection, ino: i64) -> Result<FileStorage> {
    let mut stmt = conn.prepare_cached(
        "SELECT ino, mode, nlink, uid, gid, size, atime, mtime, ctime, rdev,
                    atime_nsec, mtime_nsec, ctime_nsec, data_inline, storage_kind
             FROM fs_inode WHERE ino = ?",
    )?;
    let mut rows = stmt.query((ino,))?;

    if let Some(row) = rows.next()? {
        let inode = InodeRow::from_row(row, 0)?;
        let storage_kind = inode.storage_kind;
        if storage_kind != STORAGE_CHUNKED && storage_kind != STORAGE_INLINE {
            return Err(corrupt_column("storage_kind", "unknown storage kind"));
        }
        if storage_kind == STORAGE_INLINE && inode.data_inline.is_none() {
            return Err(corrupt_column(
                "data_inline",
                "inline file missing inline data",
            ));
        }
        Ok(FileStorage { inode })
    } else {
        Err(FsError::NotFound.into())
    }
}

// Architecture §2.1 exposes `read` as the free-function store seam. The
// VfsFile hot path uses `read_from_storage` after it has already fetched
// metadata, avoiding a duplicate metadata query.
#[allow(dead_code)]
pub(in crate::fs) fn read(
    conn: &Connection,
    ino: i64,
    geometry: Geometry,
    offset: u64,
    size: u64,
) -> Result<Vec<u8>> {
    let metadata = file_storage(conn, ino)?;
    read_from_storage(conn, ino, geometry, &metadata, offset, size)
}

pub(super) fn read_from_storage(
    conn: &Connection,
    ino: i64,
    geometry: Geometry,
    metadata: &FileStorage,
    offset: u64,
    size: u64,
) -> Result<Vec<u8>> {
    if offset >= metadata.size() || size == 0 {
        return Ok(Vec::new());
    }

    let size = std::cmp::min(size, metadata.size() - offset);
    if metadata.storage_kind() == STORAGE_INLINE {
        let mut result = Vec::with_capacity(size as usize);
        let inline_data = metadata.inode.data_inline.as_deref().unwrap_or_default();
        let start = offset as usize;
        let requested = size as usize;

        if start < inline_data.len() {
            let available = std::cmp::min(inline_data.len() - start, requested);
            result.extend_from_slice(&inline_data[start..start + available]);
        }

        if result.len() < requested {
            result.resize(requested, 0);
        }

        return Ok(result);
    }

    read_chunked(conn, ino, geometry, offset, size)
}

/// Splices ordered chunk rows into one read buffer, zero-filling sparse gaps.
struct ChunkAssembler {
    result: Vec<u8>,
    size: usize,
    chunk_size: usize,
    start_chunk: u64,
    start_offset_in_chunk: usize,
    next_expected_chunk: u64,
    chunks_read: u64,
}

impl ChunkAssembler {
    fn new(offset: u64, size: u64, chunk_size: u64) -> Self {
        Self {
            result: Vec::with_capacity(size as usize),
            size: size as usize,
            chunk_size: chunk_size as usize,
            start_chunk: offset / chunk_size,
            start_offset_in_chunk: (offset % chunk_size) as usize,
            next_expected_chunk: offset / chunk_size,
            chunks_read: 0,
        }
    }

    fn skip_for(&self, chunk_index: u64) -> usize {
        if chunk_index == self.start_chunk {
            self.start_offset_in_chunk
        } else {
            0
        }
    }

    fn append(&mut self, chunk_index: u64, chunk_data: &[u8]) {
        self.chunks_read += 1;

        while self.next_expected_chunk < chunk_index && self.result.len() < self.size {
            let skip = self.skip_for(self.next_expected_chunk);
            let zeros_needed = std::cmp::min(self.chunk_size - skip, self.size - self.result.len());
            self.result.extend(std::iter::repeat_n(0u8, zeros_needed));
            self.next_expected_chunk += 1;
        }

        let skip = self.skip_for(chunk_index);
        if skip >= chunk_data.len() {
            let zeros_needed = std::cmp::min(self.chunk_size - skip, self.size - self.result.len());
            self.result.extend(std::iter::repeat_n(0u8, zeros_needed));
        } else {
            let remaining = self.size - self.result.len();
            let take = std::cmp::min(chunk_data.len() - skip, remaining);
            self.result
                .extend_from_slice(&chunk_data[skip..skip + take]);

            let chunk_end = skip + take;
            if chunk_end < self.chunk_size && self.result.len() < self.size {
                let zeros_needed =
                    std::cmp::min(self.chunk_size - chunk_end, self.size - self.result.len());
                self.result.extend(std::iter::repeat_n(0u8, zeros_needed));
            }
        }
        self.next_expected_chunk = chunk_index + 1;
    }

    fn finish(mut self) -> Vec<u8> {
        if self.result.len() < self.size {
            self.result.resize(self.size, 0);
        }
        crate::telemetry::record_chunk_read_chunks(self.chunks_read);
        self.result
    }
}

fn read_chunked(
    conn: &Connection,
    ino: i64,
    geometry: Geometry,
    offset: u64,
    size: u64,
) -> Result<Vec<u8>> {
    let chunk_size = geometry.chunk_size as u64;
    let start_chunk = offset / chunk_size;
    let end_chunk = (offset + size).saturating_sub(1) / chunk_size;
    let mut assembler = ChunkAssembler::new(offset, size, chunk_size);
    crate::telemetry::record_chunk_read_query();

    {
        let mut stmt = conn.prepare_cached(
            "SELECT d.chunk_index, c.data
                 FROM fs_data d
                 JOIN fs_chunk c ON c.digest = d.digest
                 WHERE d.ino = ? AND d.chunk_index BETWEEN ? AND ?
                 ORDER BY d.chunk_index",
        )?;
        let mut rows = stmt.query((ino, start_chunk as i64, end_chunk as i64))?;

        while let Some(row) = rows.next()? {
            let chunk_index = required_u64(row, 0, "fs_data.chunk_index")?;
            let data = match row.get_ref(1)? {
                tokio_rusqlite::rusqlite::types::ValueRef::Blob(data) => data,
                _ => return Err(corrupt_column("fs_chunk.data", "expected blob")),
            };
            assembler.append(chunk_index, data);
        }
    }

    Ok(assembler.finish())
}

fn digest_chunk(data: &[u8]) -> Vec<u8> {
    blake3::hash(data).as_bytes().to_vec()
}

fn mapping_digest(conn: &Connection, ino: i64, chunk_index: i64) -> Result<Option<Vec<u8>>> {
    let mut stmt =
        conn.prepare_cached("SELECT digest FROM fs_data WHERE ino = ? AND chunk_index = ?")?;
    let mut rows = stmt.query((ino, chunk_index))?;
    let Some(row) = rows.next()? else {
        return Ok(None);
    };
    match row.get::<_, Value>(0)? {
        Value::Blob(digest) => Ok(Some(digest)),
        _ => Err(corrupt_column("fs_data.digest", "expected blob")),
    }
}

fn upsert_chunk(conn: &Connection, digest: &[u8], data: &[u8]) -> Result<()> {
    conn.execute(
        "INSERT INTO fs_chunk (digest, data, refcount)
         VALUES (?, ?, 1)
         ON CONFLICT(digest) DO UPDATE SET refcount = refcount + 1",
        (digest, data),
    )?;
    Ok(())
}

fn decrement_chunk_refcount(conn: &Connection, digest: &[u8], count: i64) -> Result<()> {
    conn.execute(
        "UPDATE fs_chunk SET refcount = refcount - ? WHERE digest = ?",
        (count, digest),
    )?;
    Ok(())
}

pub(super) fn insert_chunk_mapping(
    conn: &Connection,
    ino: i64,
    chunk_index: i64,
    data: &[u8],
) -> Result<Vec<u8>> {
    insert_chunk_mapping_inner(conn, ino, chunk_index, data, None)
}

fn insert_chunk_mapping_inner(
    conn: &Connection,
    ino: i64,
    chunk_index: i64,
    data: &[u8],
    mut deltas: Option<&mut Vec<DataDelta>>,
) -> Result<Vec<u8>> {
    let digest = digest_chunk(data);
    let old_digest = mapping_digest(conn, ino, chunk_index)?;
    if old_digest.as_deref() == Some(digest.as_slice()) {
        return Ok(digest);
    }

    upsert_chunk(conn, &digest, data)?;
    conn.execute(
        "INSERT OR REPLACE INTO fs_data (ino, chunk_index, digest) VALUES (?, ?, ?)",
        (ino, chunk_index, digest.as_slice()),
    )?;
    if let Some(old_digest) = old_digest {
        decrement_chunk_refcount(conn, &old_digest, 1)?;
    }
    if let Some(deltas) = &mut deltas {
        deltas.push(DataDelta::Upsert {
            ino,
            chunk_index,
            digest: digest.clone(),
        });
    }
    Ok(digest)
}

fn mapped_rows(
    conn: &Connection,
    sql: &str,
    params: impl tokio_rusqlite::rusqlite::Params,
) -> Result<Vec<(i64, Vec<u8>)>> {
    let mut query_statement_0 = conn.prepare_cached(sql)?;
    let mut rows = query_statement_0.query(params)?;
    let mut mappings = Vec::new();
    while let Some(row) = rows.next()? {
        let chunk_index = required_i64(row, 0, "fs_data.chunk_index")?;
        let digest = match row.get::<_, Value>(1)? {
            Value::Blob(digest) => digest,
            _ => return Err(corrupt_column("fs_data.digest", "expected blob")),
        };
        mappings.push((chunk_index, digest));
    }
    Ok(mappings)
}

pub(super) fn delete_all_chunk_mappings(conn: &Connection, ino: i64) -> Result<Vec<DataDelta>> {
    let mappings = mapped_rows(
        conn,
        "SELECT chunk_index, digest FROM fs_data WHERE ino = ? ORDER BY chunk_index",
        (ino,),
    )?;
    let mut deltas = Vec::with_capacity(mappings.len());
    let mut counts = BTreeMap::<Vec<u8>, i64>::new();
    for (chunk_index, digest) in mappings {
        deltas.push(DataDelta::Delete { ino, chunk_index });
        *counts.entry(digest).or_insert(0) += 1;
    }
    // Refcounts move first so every mapping deletion has a matching decrement
    // in the caller's transaction.
    for (digest, count) in counts {
        decrement_chunk_refcount(conn, &digest, count)?;
    }
    conn.execute("DELETE FROM fs_data WHERE ino = ?", (ino,))?;
    Ok(deltas)
}

fn delete_chunk_mappings_after(
    conn: &Connection,
    ino: i64,
    last_chunk_index: i64,
) -> Result<Vec<DataDelta>> {
    let mappings = mapped_rows(
        conn,
        "SELECT chunk_index, digest
         FROM fs_data
         WHERE ino = ? AND chunk_index > ?
         ORDER BY chunk_index",
        (ino, last_chunk_index),
    )?;
    let mut deltas = Vec::with_capacity(mappings.len());
    let mut counts = BTreeMap::<Vec<u8>, i64>::new();
    for (chunk_index, digest) in mappings {
        deltas.push(DataDelta::Delete { ino, chunk_index });
        *counts.entry(digest).or_insert(0) += 1;
    }
    for (digest, count) in counts {
        decrement_chunk_refcount(conn, &digest, count)?;
    }
    conn.execute(
        "DELETE FROM fs_data WHERE ino = ? AND chunk_index > ?",
        (ino, last_chunk_index),
    )?;
    Ok(deltas)
}

/// `preserve_times`: when true (deferred batcher commits racing an explicit
/// chmod/chown/utimens), leave mtime/ctime untouched instead of stamping
/// the commit time. `explicit_times`: stashed setattr values folded into the
/// inode UPDATE itself (see `write_commit_time_sets`).
struct WriteOptions<'a> {
    preserve_times: bool,
    explicit_times: Option<&'a PendingTimeChange>,
    hooks: Option<&'a dyn ChunkWriteHooks>,
    force_chunked: bool,
}

pub(super) fn write_ranges(
    conn: &Connection,
    ino: i64,
    geometry: Geometry,
    ranges: &[WriteRangeRef<'_>],
    preserve_times: bool,
    explicit_times: Option<&PendingTimeChange>,
) -> Result<StorageChanges> {
    write_ranges_inner(
        conn,
        ino,
        geometry,
        ranges,
        WriteOptions {
            preserve_times,
            explicit_times,
            hooks: None,
            force_chunked: false,
        },
    )
}

pub(in crate::fs) fn write_ranges_with_chunk_hooks(
    conn: &Connection,
    ino: i64,
    geometry: Geometry,
    ranges: &[WriteRangeRef<'_>],
    hooks: &dyn ChunkWriteHooks,
) -> Result<StorageChanges> {
    write_ranges_inner(
        conn,
        ino,
        geometry,
        ranges,
        WriteOptions {
            preserve_times: false,
            explicit_times: None,
            hooks: Some(hooks),
            force_chunked: true,
        },
    )
}

fn write_ranges_inner(
    conn: &Connection,
    ino: i64,
    geometry: Geometry,
    ranges: &[WriteRangeRef<'_>],
    options: WriteOptions<'_>,
) -> Result<StorageChanges> {
    let ranges = normalize_write_ranges(ranges)?;
    if ranges.is_empty() {
        return Err(Error::Internal(
            "write_ranges_inner called without data".to_string(),
        ));
    }

    let mut metadata = file_storage(conn, ino)?;
    let mut data_deltas = Vec::new();
    let write_end = ranges
        .iter()
        .map(NormalizedWriteRange::end)
        .max()
        .unwrap_or(metadata.size());
    let new_size = std::cmp::max(metadata.size(), write_end);

    if !options.force_chunked
        && metadata.storage_kind() == STORAGE_INLINE
        && new_size <= geometry.inline_threshold as u64
        && dense_after_inline_write_batch(metadata.size(), new_size, &ranges)
    {
        let mut inline_data = metadata.inline_data().unwrap_or_default();
        inline_data.resize(metadata.size() as usize, 0);
        inline_data.resize(new_size as usize, 0);
        for range in &ranges {
            let start = range.offset as usize;
            inline_data[start..start + range.data.len()].copy_from_slice(&range.data);
        }

        data_deltas.extend(delete_all_chunk_mappings(conn, ino)?);
        let mut sets = vec!["size = ?", "data_inline = ?", "storage_kind = ?"];
        let mut values: Vec<Value> = vec![
            Value::Integer(new_size as i64),
            Value::Blob(inline_data.clone()),
            Value::Integer(STORAGE_INLINE),
        ];
        let (time_sets, time_values, resolved_times) =
            write_commit_time_sets(options.preserve_times, options.explicit_times)?;
        sets.extend(time_sets);
        values.extend(time_values);
        values.push(Value::Integer(ino));
        let sql = format!("UPDATE fs_inode SET {} WHERE ino = ?", sets.join(", "));
        conn.execute(&sql, tokio_rusqlite::rusqlite::params_from_iter(values))?;
        metadata.inode.size = new_size as i64;
        metadata.inode.data_inline = Some(inline_data);
        metadata.inode.storage_kind = STORAGE_INLINE;
        apply_resolved_times(&mut metadata.inode, resolved_times);
        return Ok(StorageChanges {
            inode: metadata.inode,
            data: data_deltas,
        });
    }

    let mut chunked_ranges = Vec::new();
    if metadata.storage_kind() == STORAGE_INLINE {
        let mut inline_data = metadata.inline_data().unwrap_or_default();
        inline_data.resize(metadata.size() as usize, 0);
        data_deltas.extend(delete_all_chunk_mappings(conn, ino)?);
        if !inline_data.is_empty() {
            chunked_ranges.push(NormalizedWriteRange {
                offset: 0,
                data: inline_data,
            });
        }
    } else {
        conn.execute(
            "UPDATE fs_inode SET data_inline = NULL, storage_kind = ? WHERE ino = ?",
            (STORAGE_CHUNKED, ino),
        )?;
    }

    chunked_ranges.extend(ranges);
    write_ranges_chunked(
        conn,
        ino,
        geometry,
        &chunked_ranges,
        options.hooks,
        &mut data_deltas,
    )?;

    let mut sets = vec!["size = ?", "data_inline = NULL", "storage_kind = ?"];
    let mut values: Vec<Value> = vec![
        Value::Integer(new_size as i64),
        Value::Integer(STORAGE_CHUNKED),
    ];
    let (time_sets, time_values, resolved_times) =
        write_commit_time_sets(options.preserve_times, options.explicit_times)?;
    sets.extend(time_sets);
    values.extend(time_values);
    values.push(Value::Integer(ino));
    let sql = format!("UPDATE fs_inode SET {} WHERE ino = ?", sets.join(", "));
    conn.execute(&sql, tokio_rusqlite::rusqlite::params_from_iter(values))?;

    metadata.inode.size = new_size as i64;
    metadata.inode.data_inline = None;
    metadata.inode.storage_kind = STORAGE_CHUNKED;
    apply_resolved_times(&mut metadata.inode, resolved_times);
    Ok(StorageChanges {
        inode: metadata.inode,
        data: data_deltas,
    })
}

pub(super) fn truncate(
    conn: &Connection,
    ino: i64,
    geometry: Geometry,
    new_size: u64,
) -> Result<StorageChanges> {
    truncate_inner(conn, ino, geometry, new_size, false, None)
}

pub(in crate::fs) fn truncate_with_chunk_hooks(
    conn: &Connection,
    ino: i64,
    geometry: Geometry,
    new_size: u64,
    hooks: &dyn ChunkWriteHooks,
) -> Result<StorageChanges> {
    truncate_inner(conn, ino, geometry, new_size, true, Some(hooks))
}

fn truncate_inner(
    conn: &Connection,
    ino: i64,
    geometry: Geometry,
    new_size: u64,
    force_chunked: bool,
    hooks: Option<&dyn ChunkWriteHooks>,
) -> Result<StorageChanges> {
    let mut metadata = file_storage(conn, ino)?;
    let mut data_deltas = Vec::new();

    if metadata.storage_kind() == STORAGE_INLINE {
        if !force_chunked && new_size <= geometry.inline_threshold as u64 {
            let mut inline_data = metadata.inline_data().unwrap_or_default();
            inline_data.resize(metadata.size() as usize, 0);
            inline_data.resize(new_size as usize, 0);
            data_deltas.extend(delete_all_chunk_mappings(conn, ino)?);
            let (now_secs, now_nsec) = current_timestamp()?;
            conn.execute(
                "UPDATE fs_inode SET size = ?, data_inline = ?, storage_kind = ?, mtime = ?, ctime = ?, mtime_nsec = ?, ctime_nsec = ? WHERE ino = ?",
                (
                    new_size as i64,
                    Value::Blob(inline_data.clone()),
                    STORAGE_INLINE,
                    now_secs,
                    now_secs,
                    now_nsec,
                    now_nsec,
                    ino,
                ),
            )
            ?;
            metadata.inode.size = new_size as i64;
            metadata.inode.data_inline = Some(inline_data);
            metadata.inode.storage_kind = STORAGE_INLINE;
            metadata.inode.mtime = now_secs;
            metadata.inode.ctime = now_secs;
            metadata.inode.mtime_nsec = now_nsec;
            metadata.inode.ctime_nsec = now_nsec;
            return Ok(StorageChanges {
                inode: metadata.inode,
                data: data_deltas,
            });
        }

        let mut inline_data = metadata.inline_data().unwrap_or_default();
        inline_data.resize(metadata.size() as usize, 0);
        transition_inline_to_chunked(conn, ino, geometry, &inline_data, hooks, &mut data_deltas)?;
        truncate_chunked_data(
            conn,
            ino,
            geometry,
            metadata.size(),
            new_size,
            hooks,
            &mut data_deltas,
        )?;
        update_chunked_truncate_metadata(conn, &mut metadata.inode, new_size)?;
        return Ok(StorageChanges {
            inode: metadata.inode,
            data: data_deltas,
        });
    }

    if !force_chunked && new_size <= geometry.inline_threshold as u64 {
        if let Some(inline_data) = read_dense_prefix_for_inline(conn, ino, geometry, new_size)? {
            data_deltas.extend(delete_all_chunk_mappings(conn, ino)?);
            let (now_secs, now_nsec) = current_timestamp()?;
            conn.execute(
                "UPDATE fs_inode SET size = ?, data_inline = ?, storage_kind = ?, mtime = ?, ctime = ?, mtime_nsec = ?, ctime_nsec = ? WHERE ino = ?",
                (
                    new_size as i64,
                    Value::Blob(inline_data.clone()),
                    STORAGE_INLINE,
                    now_secs,
                    now_secs,
                    now_nsec,
                    now_nsec,
                    ino,
                ),
            )
            ?;
            metadata.inode.size = new_size as i64;
            metadata.inode.data_inline = Some(inline_data);
            metadata.inode.storage_kind = STORAGE_INLINE;
            metadata.inode.mtime = now_secs;
            metadata.inode.ctime = now_secs;
            metadata.inode.mtime_nsec = now_nsec;
            metadata.inode.ctime_nsec = now_nsec;
            return Ok(StorageChanges {
                inode: metadata.inode,
                data: data_deltas,
            });
        }
    }

    truncate_chunked_data(
        conn,
        ino,
        geometry,
        metadata.size(),
        new_size,
        hooks,
        &mut data_deltas,
    )?;
    update_chunked_truncate_metadata(conn, &mut metadata.inode, new_size)?;
    Ok(StorageChanges {
        inode: metadata.inode,
        data: data_deltas,
    })
}

fn transition_inline_to_chunked(
    conn: &Connection,
    ino: i64,
    geometry: Geometry,
    inline_data: &[u8],
    hooks: Option<&dyn ChunkWriteHooks>,
    data_deltas: &mut Vec<DataDelta>,
) -> Result<()> {
    data_deltas.extend(delete_all_chunk_mappings(conn, ino)?);

    if !inline_data.is_empty() {
        write_data_at_offset(conn, ino, geometry, 0, inline_data, hooks, data_deltas)?;
    }

    conn.execute(
        "UPDATE fs_inode SET data_inline = NULL, storage_kind = ? WHERE ino = ?",
        (STORAGE_CHUNKED, ino),
    )?;

    Ok(())
}

fn read_dense_prefix_for_inline(
    conn: &Connection,
    ino: i64,
    geometry: Geometry,
    new_size: u64,
) -> Result<Option<Vec<u8>>> {
    if new_size == 0 {
        return Ok(Some(Vec::new()));
    }

    let chunk_size = geometry.chunk_size as u64;
    let last_chunk = (new_size - 1) / chunk_size;
    let mut inline_data = Vec::with_capacity(new_size as usize);

    let mut stmt = conn.prepare_cached(
        "SELECT c.data
             FROM fs_data d
             JOIN fs_chunk c ON c.digest = d.digest
             WHERE d.ino = ? AND d.chunk_index = ?",
    )?;
    for chunk_idx in 0..=last_chunk {
        let mut rows = stmt.query((ino, chunk_idx as i64))?;
        let Some(row) = rows.next()? else {
            return Ok(None);
        };
        let data = match row.get::<_, Value>(0)? {
            Value::Blob(data) => data,
            _ => return Err(corrupt_column("fs_chunk.data", "expected blob")),
        };
        drop(rows);
        let chunk_data = data;
        let remaining = new_size as usize - inline_data.len();
        let needed = std::cmp::min(geometry.chunk_size, remaining);
        if chunk_data.len() < needed {
            return Ok(None);
        }
        inline_data.extend_from_slice(&chunk_data[..needed]);
    }

    Ok(Some(inline_data))
}

fn truncate_chunked_data(
    conn: &Connection,
    ino: i64,
    geometry: Geometry,
    current_size: u64,
    new_size: u64,
    hooks: Option<&dyn ChunkWriteHooks>,
    data_deltas: &mut Vec<DataDelta>,
) -> Result<()> {
    let chunk_size = geometry.chunk_size as u64;

    if new_size == 0 {
        data_deltas.extend(delete_all_chunk_mappings(conn, ino)?);
    } else if new_size < current_size {
        let last_chunk_idx = (new_size - 1) / chunk_size;

        data_deltas.extend(delete_chunk_mappings_after(
            conn,
            ino,
            last_chunk_idx as i64,
        )?);

        let end_in_last_chunk = ((new_size - 1) % chunk_size + 1) as usize;
        if end_in_last_chunk < chunk_size as usize {
            let mut stmt = conn.prepare_cached(
                "SELECT c.data
                     FROM fs_data d
                     JOIN fs_chunk c ON c.digest = d.digest
                     WHERE d.ino = ? AND d.chunk_index = ?",
            )?;
            let mut rows = stmt.query((ino, last_chunk_idx as i64))?;

            if let Some(row) = rows.next()? {
                let data = match row.get::<_, Value>(0)? {
                    Value::Blob(data) => data,
                    _ => return Err(corrupt_column("fs_chunk.data", "expected blob")),
                };
                drop(rows);
                let chunk_data = data;
                if chunk_data.len() > end_in_last_chunk {
                    insert_chunk_mapping_inner(
                        conn,
                        ino,
                        last_chunk_idx as i64,
                        &chunk_data[..end_in_last_chunk],
                        Some(data_deltas),
                    )?;
                    if let Some(hooks) = hooks {
                        hooks.chunk_written(conn, ino, last_chunk_idx as i64)?;
                    }
                }
            }
        }
    } else if new_size > current_size {
        let last_existing_chunk = if current_size == 0 {
            None
        } else {
            Some((current_size - 1) / chunk_size)
        };
        let last_new_chunk = (new_size - 1) / chunk_size;

        if let Some(last_idx) = last_existing_chunk {
            let mut stmt = conn.prepare_cached(
                "SELECT c.data
                     FROM fs_data d
                     JOIN fs_chunk c ON c.digest = d.digest
                     WHERE d.ino = ? AND d.chunk_index = ?",
            )?;
            let mut rows = stmt.query((ino, last_idx as i64))?;

            if let Some(row) = rows.next()? {
                let data = match row.get::<_, Value>(0)? {
                    Value::Blob(data) => data,
                    _ => return Err(corrupt_column("fs_chunk.data", "expected blob")),
                };
                drop(rows);
                let chunk_data = data;
                let current_chunk_len = chunk_data.len();
                let needed_len = if last_idx == last_new_chunk {
                    ((new_size - 1) % chunk_size + 1) as usize
                } else {
                    chunk_size as usize
                };

                if needed_len > current_chunk_len {
                    let mut padded = chunk_data.clone();
                    padded.resize(needed_len, 0);
                    insert_chunk_mapping_inner(
                        conn,
                        ino,
                        last_idx as i64,
                        &padded,
                        Some(data_deltas),
                    )?;
                    if let Some(hooks) = hooks {
                        hooks.chunk_written(conn, ino, last_idx as i64)?;
                    }
                }
            }
        }

        let start_new_chunk = last_existing_chunk.map(|i| i + 1).unwrap_or(0);
        for chunk_idx in start_new_chunk..=last_new_chunk {
            let chunk_len = if chunk_idx == last_new_chunk {
                ((new_size - 1) % chunk_size + 1) as usize
            } else {
                chunk_size as usize
            };
            let zeros = vec![0u8; chunk_len];
            insert_chunk_mapping_inner(conn, ino, chunk_idx as i64, &zeros, Some(data_deltas))?;
            if let Some(hooks) = hooks {
                hooks.chunk_written(conn, ino, chunk_idx as i64)?;
            }
        }
    }

    Ok(())
}

fn update_chunked_truncate_metadata(
    conn: &Connection,
    inode: &mut InodeRow,
    new_size: u64,
) -> Result<()> {
    let (now_secs, now_nsec) = current_timestamp()?;
    conn.execute(
        "UPDATE fs_inode SET size = ?, data_inline = NULL, storage_kind = ?, mtime = ?, ctime = ?, mtime_nsec = ?, ctime_nsec = ? WHERE ino = ?",
        (
            new_size as i64,
            STORAGE_CHUNKED,
            now_secs,
            now_secs,
            now_nsec,
            now_nsec,
            inode.ino,
        ),
    )
    ?;
    inode.size = new_size as i64;
    inode.data_inline = None;
    inode.storage_kind = STORAGE_CHUNKED;
    inode.mtime = now_secs;
    inode.ctime = now_secs;
    inode.mtime_nsec = now_nsec;
    inode.ctime_nsec = now_nsec;
    Ok(())
}

fn write_data_at_offset(
    conn: &Connection,
    ino: i64,
    geometry: Geometry,
    offset: u64,
    data: &[u8],
    hooks: Option<&dyn ChunkWriteHooks>,
    data_deltas: &mut Vec<DataDelta>,
) -> Result<()> {
    let ranges = [WriteRangeRef { offset, data }];
    let ranges = normalize_write_ranges(&ranges)?;
    write_ranges_chunked(conn, ino, geometry, &ranges, hooks, data_deltas)
}

fn write_ranges_chunked(
    conn: &Connection,
    ino: i64,
    geometry: Geometry,
    ranges: &[NormalizedWriteRange],
    hooks: Option<&dyn ChunkWriteHooks>,
    data_deltas: &mut Vec<DataDelta>,
) -> Result<()> {
    let chunk_size = geometry.chunk_size as u64;

    if ranges.is_empty() {
        return Ok(());
    }

    let mut select_stmt = conn.prepare_cached(
        "SELECT c.data
             FROM fs_data d
             JOIN fs_chunk c ON c.digest = d.digest
             WHERE d.ino = ? AND d.chunk_index = ?",
    )?;

    let mut chunks: BTreeMap<i64, Vec<u8>> = BTreeMap::new();

    for range in ranges {
        let mut written = 0usize;
        while written < range.data.len() {
            let current_offset = range.offset + written as u64;
            let chunk_index = (current_offset / chunk_size) as i64;
            let offset_in_chunk = (current_offset % chunk_size) as usize;

            let remaining_in_chunk = geometry.chunk_size - offset_in_chunk;
            let remaining_data = range.data.len() - written;
            let to_write = std::cmp::min(remaining_in_chunk, remaining_data);
            let write_slice = &range.data[written..written + to_write];

            if offset_in_chunk == 0 && to_write == geometry.chunk_size {
                chunks.insert(chunk_index, write_slice.to_vec());
                written += to_write;
                continue;
            }

            if let std::collections::btree_map::Entry::Vacant(entry) = chunks.entry(chunk_index) {
                let mut rows = select_stmt.query((ino, chunk_index))?;
                let existing_chunk = if let Some(row) = rows.next()? {
                    let data = match row.get::<_, Value>(0)? {
                        Value::Blob(data) => data,
                        _ => return Err(corrupt_column("fs_chunk.data", "expected blob")),
                    };
                    Some(data)
                } else {
                    None
                };
                drop(rows);
                let chunk_data = if let Some(data) = existing_chunk {
                    data
                } else if let Some(hooks) = hooks {
                    hooks
                        .seed_missing_chunk(conn, ino, geometry, chunk_index)?
                        .unwrap_or_default()
                } else {
                    Vec::new()
                };
                entry.insert(chunk_data);
            }

            let chunk_data = chunks
                .get_mut(&chunk_index)
                .expect("chunk must be loaded before partial write");
            if chunk_data.len() < offset_in_chunk + to_write {
                chunk_data.resize(offset_in_chunk + to_write, 0);
            }
            chunk_data[offset_in_chunk..offset_in_chunk + to_write].copy_from_slice(write_slice);

            written += to_write;
        }
    }

    let chunks_written = chunks.len() as u64;
    for (chunk_index, chunk_data) in chunks {
        insert_chunk_mapping_inner(conn, ino, chunk_index, &chunk_data, Some(data_deltas))?;
        if let Some(hooks) = hooks {
            hooks.chunk_written(conn, ino, chunk_index)?;
        }
    }

    crate::telemetry::record_chunk_write_chunks(chunks_written);
    Ok(())
}

fn apply_resolved_times(inode: &mut InodeRow, times: super::batcher::ResolvedWriteTimes) {
    if let Some((secs, nsec)) = times.atime {
        inode.atime = secs;
        inode.atime_nsec = nsec;
    }
    if let Some((secs, nsec)) = times.mtime {
        inode.mtime = secs;
        inode.mtime_nsec = nsec;
    }
    if let Some((secs, nsec)) = times.ctime {
        inode.ctime = secs;
        inode.ctime_nsec = nsec;
    }
}

pub(super) fn link_count(conn: &Connection, ino: i64) -> Result<u32> {
    let mut stmt = conn.prepare_cached("SELECT nlink FROM fs_inode WHERE ino = ?")?;
    let mut rows = stmt.query((ino,))?;

    if let Some(row) = rows.next()? {
        required_u32(row, 0, "nlink")
    } else {
        Ok(0)
    }
}

pub(super) fn mode(conn: &Connection, ino: i64) -> Result<Option<u32>> {
    let mut stmt = conn.prepare_cached("SELECT mode FROM fs_inode WHERE ino = ?")?;
    let mut rows = stmt.query((ino,))?;

    if let Some(row) = rows.next()? {
        Ok(Some(required_u32(row, 0, "mode")?))
    } else {
        Ok(None)
    }
}

pub(super) fn getattr(conn: &Connection, ino: i64) -> Result<Option<Stats>> {
    let mut stmt = conn
        .prepare_cached("SELECT ino, mode, nlink, uid, gid, size, atime, mtime, ctime, rdev, atime_nsec, mtime_nsec, ctime_nsec FROM fs_inode WHERE ino = ?")
        ?;
    let mut rows = stmt.query((ino,))?;

    if let Some(row) = rows.next()? {
        Ok(Some(stats_from_row(row)?))
    } else {
        Ok(None)
    }
}

pub(super) fn stats_from_row(row: &tokio_rusqlite::rusqlite::Row<'_>) -> Result<Stats> {
    stats_from_row_at(row, 0)
}

pub(super) fn stats_from_row_at(
    row: &tokio_rusqlite::rusqlite::Row<'_>,
    start: usize,
) -> Result<Stats> {
    let size = required_i64(row, start + 5, "size")?;
    if size < 0 {
        return Err(corrupt_column("size", "negative file size"));
    }
    Ok(Stats {
        ino: required_i64(row, start, "ino")?,
        mode: required_u32(row, start + 1, "mode")?,
        nlink: required_u32(row, start + 2, "nlink")?,
        uid: required_u32(row, start + 3, "uid")?,
        gid: required_u32(row, start + 4, "gid")?,
        size,
        atime: required_i64(row, start + 6, "atime")?,
        mtime: required_i64(row, start + 7, "mtime")?,
        ctime: required_i64(row, start + 8, "ctime")?,
        atime_nsec: required_u32(row, start + 10, "atime_nsec")?,
        mtime_nsec: required_u32(row, start + 11, "mtime_nsec")?,
        ctime_nsec: required_u32(row, start + 12, "ctime_nsec")?,
        rdev: required_u64(row, start + 9, "rdev")?,
    })
}

fn required_i64(
    row: &tokio_rusqlite::rusqlite::Row<'_>,
    index: usize,
    column: &str,
) -> Result<i64> {
    (match row.get::<_, Value>(index)? {
        Value::Integer(n) => Some(n),
        _ => None,
    })
    .ok_or_else(|| corrupt_column(column, "expected integer"))
}

fn required_u32(
    row: &tokio_rusqlite::rusqlite::Row<'_>,
    index: usize,
    column: &str,
) -> Result<u32> {
    let value = required_i64(row, index, column)?;
    u32::try_from(value).map_err(|_| corrupt_column(column, "integer out of u32 range"))
}

fn required_u64(
    row: &tokio_rusqlite::rusqlite::Row<'_>,
    index: usize,
    column: &str,
) -> Result<u64> {
    let value = required_i64(row, index, column)?;
    u64::try_from(value).map_err(|_| corrupt_column(column, "negative integer"))
}

fn corrupt_column(column: &str, reason: &str) -> Error {
    Error::Fs(FsError::Corrupt(format!("invalid {column}: {reason}")))
}
