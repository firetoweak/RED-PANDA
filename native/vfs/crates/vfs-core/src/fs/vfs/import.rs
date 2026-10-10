use super::*;
use crate::error::Error;
use std::{collections::HashMap, sync::Arc};
use tokio_rusqlite::rusqlite::types::Value;
struct JournalImport {
    inode: InodeRow,
    dentry_id: i64,
    parent_ino: i64,
    name: String,
    symlink_target: Option<String>,
    data: Vec<(i64, Vec<u8>)>,
}

/// One node accepted by [`ImportSession::import_chunk`]. `path` is relative to
/// the import root and '/'-separated; parents must precede their children.
#[derive(Debug, Clone)]
pub struct ImportEntry {
    pub path: String,
    /// Full `st_mode` bits (S_IFDIR / S_IFREG / S_IFLNK plus permissions).
    pub mode: u32,
    /// File content, or the symlink target bytes; empty for directories.
    pub data: Vec<u8>,
}

/// Result row for one imported node: echoes the exact `ino`/`mode`/`size`
/// the filesystem will serve so callers can fabricate externally-consistent
/// stat metadata (e.g. a git index) without re-reading content.
#[derive(Debug, Clone)]
pub struct ImportedEntry {
    pub path: String,
    pub ino: i64,
    pub mode: u32,
    pub size: u64,
}

/// Ownership and timestamps applied to every node of one bulk import.
#[derive(Debug, Clone)]
pub struct ImportOptions {
    pub uid: u32,
    pub gid: u32,
    /// (secs, nanos) stamped as atime/mtime/ctime on every imported inode.
    pub timestamp: (i64, i64),
}

#[derive(Default)]
struct ImportState {
    dir_inos: HashMap<String, i64>,
    results: Vec<ImportedEntry>,
}
pub struct ImportSession {
    fs: Vfs,
    dest_parent: i64,
    opts: ImportOptions,
    state: Arc<parking_lot::Mutex<ImportState>>,
    order: Arc<tokio::sync::Mutex<()>>,
}
impl ImportSession {
    pub async fn import_chunk(&mut self, entries: &[ImportEntry]) -> Result<()> {
        let order = self.order.clone().lock_owned().await;
        let fs = self.fs.clone();
        let state = self.state.clone();
        let parent = self.dest_parent;
        let options = self.opts.clone();
        let entries = entries.to_vec();
        self.fs
            .pool
            .execute(move |conn| {
                let _order = order;
                let mut state = state.lock();
                let ImportState { dir_inos, results } = &mut *state;
                fs.import_chunk_with_conn(conn, parent, &options, dir_inos, results, &entries)
            })
            .await
    }
    pub async fn finish(self) -> Result<Vec<ImportedEntry>> {
        let _order = self.order.lock().await;
        self.fs.pool.barrier().await?;
        self.fs.invalidate_attr(self.dest_parent);
        Ok(std::mem::take(&mut self.state.lock().results))
    }
}
impl Vfs {
    pub async fn import_entries(
        &self,
        dest_parent: i64,
        entries: &[ImportEntry],
        opts: &ImportOptions,
    ) -> Result<Vec<ImportedEntry>> {
        let mut session = self.begin_import(dest_parent, opts.clone()).await?;
        session.import_chunk(entries).await?;
        session.finish().await
    }
    pub async fn begin_import(
        &self,
        dest_parent: i64,
        opts: ImportOptions,
    ) -> Result<ImportSession> {
        self.check_background()?;
        Ok(ImportSession {
            fs: self.clone(),
            dest_parent,
            opts,
            state: Arc::default(),
            order: Arc::default(),
        })
    }
    fn import_chunk_with_conn(
        &self,
        conn: &Connection,
        dest_parent: i64,
        opts: &ImportOptions,
        dir_inos: &mut HashMap<String, i64>,
        results: &mut Vec<ImportedEntry>,
        entries: &[ImportEntry],
    ) -> Result<()> {
        let max_inodes = self.core_config.batcher.txn_max_inodes.max(1);
        let max_bytes = self.core_config.batcher.txn_max_bytes.max(1);
        let (ts_secs, ts_nsec) = opts.timestamp;

        let mut inode_stmt = conn
            .prepare_cached(
                "INSERT INTO fs_inode (mode, nlink, uid, gid, size, atime, mtime, ctime, atime_nsec, mtime_nsec, ctime_nsec, data_inline, storage_kind)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING ino",
            )
            ?;
        let mut dentry_stmt =
            conn.prepare_cached("INSERT INTO fs_dentry (name, parent_ino, ino) VALUES (?, ?, ?)")?;
        let mut symlink_stmt =
            conn.prepare_cached("INSERT INTO fs_symlink (ino, target) VALUES (?, ?)")?;
        let mut parent_stmt = conn.prepare_cached(
            "UPDATE fs_inode
                 SET nlink = nlink + ?, ctime = ?, mtime = ?, ctime_nsec = ?, mtime_nsec = ?
                 WHERE ino = ?
                 RETURNING ino, mode, nlink, uid, gid, size, atime, mtime, ctime, rdev,
                           atime_nsec, mtime_nsec, ctime_nsec, data_inline, storage_kind",
        )?;

        results.reserve(entries.len());

        let mut idx = 0usize;
        while idx < entries.len() {
            let mut batch_end = idx;
            let mut batch_bytes = 0usize;
            while batch_end < entries.len()
                && batch_end - idx < max_inodes
                && (batch_end == idx || batch_bytes + entries[batch_end].data.len() <= max_bytes)
            {
                batch_bytes += entries[batch_end].data.len();
                batch_end += 1;
            }

            // Cache fills staged until after a successful commit so a rolled
            // back batch never leaves phantom dentries/attrs behind.
            let mut staged: Vec<(i64, String, Stats)> = Vec::with_capacity(batch_end - idx);
            let mut journal_imports: Vec<JournalImport> = Vec::with_capacity(batch_end - idx);
            // parent ino -> nlink bump from new subdirectories ("..").
            let mut parent_bumps: HashMap<i64, i64> = HashMap::new();

            let mut batch_dirs = dir_inos.clone();
            let mut batch_results = Vec::new();
            let mut txn = MutationTxn::begin(conn, self.journal_ctx())?;
            for entry in &entries[idx..batch_end] {
                let (parent_path, name) = match entry.path.rsplit_once('/') {
                    Some((parent, name)) => (parent, name),
                    None => ("", entry.path.as_str()),
                };
                if name.is_empty() || name == "." || name == ".." {
                    return Err(FsError::InvalidPath.into());
                }
                if name.len() > MAX_NAME_LEN {
                    return Err(FsError::NameTooLong.into());
                }
                let parent_ino = if parent_path.is_empty() {
                    dest_parent
                } else {
                    *batch_dirs
                        .get(parent_path)
                        .ok_or_else(|| Error::Fs(FsError::NotFound))?
                };

                let kind = entry.mode & S_IFMT;
                let (nlink, size, data_inline, storage_kind) = match kind {
                    S_IFDIR => (2i64, 0u64, Value::Null, STORAGE_CHUNKED),
                    S_IFLNK => (1, entry.data.len() as u64, Value::Null, STORAGE_CHUNKED),
                    S_IFREG => {
                        if entry.data.len() <= self.inline_threshold {
                            (
                                1,
                                entry.data.len() as u64,
                                Value::Blob(entry.data.clone()),
                                STORAGE_INLINE,
                            )
                        } else {
                            (1, entry.data.len() as u64, Value::Null, STORAGE_CHUNKED)
                        }
                    }
                    _ => return Err(FsError::InvalidPath.into()),
                };

                let mut single_row_query = inode_stmt.query((
                    entry.mode as i64,
                    nlink,
                    opts.uid,
                    opts.gid,
                    size as i64,
                    ts_secs,
                    ts_secs,
                    ts_secs,
                    ts_nsec,
                    ts_nsec,
                    ts_nsec,
                    data_inline,
                    storage_kind,
                ))?;
                let row = single_row_query.next()?.ok_or(FsError::NotFound)?;
                let ino = Some(row.get::<_, i64>(0)?)
                    .ok_or_else(|| Error::Internal("failed to get inode".to_string()))?;

                match dentry_stmt.execute((name, parent_ino, ino)) {
                    Ok(_) => {}
                    Err(tokio_rusqlite::rusqlite::Error::SqliteFailure(code, _))
                        if code.code
                            == tokio_rusqlite::rusqlite::ErrorCode::ConstraintViolation =>
                    {
                        return Err(FsError::AlreadyExists.into())
                    }
                    Err(error) => return Err(error.into()),
                }
                let dentry_id = conn.last_insert_rowid();

                let (journal_digests, symlink_target) = match kind {
                    S_IFDIR => {
                        batch_dirs.insert(entry.path.clone(), ino);
                        *parent_bumps.entry(parent_ino).or_insert(0) += 1;
                        (Some(Vec::new()), None)
                    }
                    S_IFLNK => {
                        let target = std::str::from_utf8(&entry.data)
                            .map_err(|_| Error::Fs(FsError::InvalidPath))?;
                        symlink_stmt.execute((ino, target))?;
                        parent_bumps.entry(parent_ino).or_insert(0);
                        (Some(Vec::new()), Some(target.to_string()))
                    }
                    _ => {
                        let digests = if storage_kind == STORAGE_CHUNKED {
                            let mut digests =
                                Vec::with_capacity(entry.data.len().div_ceil(self.chunk_size));
                            for (chunk_index, chunk) in
                                entry.data.chunks(self.chunk_size).enumerate()
                            {
                                digests.push(super::store::insert_chunk_mapping(
                                    conn,
                                    ino,
                                    chunk_index as i64,
                                    chunk,
                                )?);
                            }
                            Some(digests)
                        } else {
                            None
                        };
                        parent_bumps.entry(parent_ino).or_insert(0);
                        (digests, None)
                    }
                };

                let stats = Stats {
                    ino,
                    mode: entry.mode,
                    nlink: nlink as u32,
                    uid: opts.uid,
                    gid: opts.gid,
                    size: size as i64,
                    atime: ts_secs,
                    mtime: ts_secs,
                    ctime: ts_secs,
                    atime_nsec: ts_nsec as u32,
                    mtime_nsec: ts_nsec as u32,
                    ctime_nsec: ts_nsec as u32,
                    rdev: 0,
                };
                if txn.journaling() {
                    journal_imports.push(JournalImport {
                        inode: InodeRow::from_stats(
                            &stats,
                            (storage_kind == STORAGE_INLINE).then(|| entry.data.clone()),
                            storage_kind,
                        ),
                        dentry_id,
                        parent_ino,
                        name: name.to_string(),
                        symlink_target,
                        data: journal_digests
                            .unwrap_or_default()
                            .into_iter()
                            .enumerate()
                            .map(|(index, digest)| (index as i64, digest))
                            .collect(),
                    });
                }

                staged.push((parent_ino, name.to_string(), stats));
                batch_results.push(ImportedEntry {
                    path: entry.path.clone(),
                    ino,
                    mode: entry.mode,
                    size,
                });
            }

            let mut parent_rows = Vec::with_capacity(parent_bumps.len());
            for (parent_ino, bump) in &parent_bumps {
                let mut single_row_query =
                    parent_stmt.query((*bump, ts_secs, ts_secs, ts_nsec, ts_nsec, *parent_ino))?;
                let row = single_row_query.next()?.ok_or(FsError::NotFound)?;
                if txn.journaling() {
                    parent_rows.push(InodeRow::from_row(row, 0)?);
                }
            }

            for import in journal_imports {
                let ino = import.inode.ino;
                txn.record_inode("import", import.inode)?;
                txn.record(JournalDelta::dentry_upsert(
                    "import",
                    import.dentry_id,
                    import.parent_ino,
                    &import.name,
                    ino,
                ));
                if let Some(target) = import.symlink_target {
                    txn.record(JournalDelta::symlink_upsert("import", ino, &target));
                }
                for (chunk_index, digest) in import.data {
                    txn.record(JournalDelta::data_upsert(
                        "import",
                        ino,
                        chunk_index,
                        digest,
                    ));
                }
            }
            for parent in parent_rows {
                txn.record_inode("import", parent)?;
            }
            txn.commit()?;
            *dir_inos = batch_dirs;
            results.extend(batch_results);
            #[cfg(test)]
            self.import_commit_sizes.lock().unwrap().push(staged.len());
            crate::telemetry::record_vfs_batcher_commit_txn(staged.len() as u64);

            for (parent_ino, name, stats) in staged {
                self.cache_dentry(parent_ino, &name, stats.ino);
                // Directories keep changing (nlink/time bumps as later batches
                // add children), so only leaf attrs are safe to prime.
                if stats.mode & S_IFMT != S_IFDIR {
                    self.cache_attr(stats);
                }
            }
            for parent_ino in parent_bumps.keys() {
                self.invalidate_attr(*parent_ino);
            }

            idx = batch_end;
        }

        Ok(())
    }
}
