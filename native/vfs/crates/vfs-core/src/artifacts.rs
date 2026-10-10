//! Immutable artifacts, overlay lineage and history maintenance.

use std::path::Path;

use tokio_rusqlite::rusqlite::{types::Value, Connection};

use crate::error::{Error, Result};
use crate::fs::vfs::{JournalDelta, MutationTxn};
use crate::Vfs;

/// Key in `fs_overlay_config` recording the sha256 of the frozen parent
/// artifact a branch session reads through. Presence makes the database a
/// branch delta: its mount shape is overlay(branch, overlay(parent, base)),
/// and the mount MUST refuse to serve if the artifact's bytes no longer hash
/// to this digest.
const PARENT_ARTIFACT_KEY: &str = "parent_artifact";

impl Vfs {
    /// Record the frozen parent artifact digest this branch delta reads
    /// through. Requires the overlay schema to be initialized.
    pub async fn set_overlay_parent_artifact(&self, digest: &str) -> Result<()> {
        self.check_background()?;

        if digest.len() != 64 || !digest.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(Error::Internal(format!(
                "invalid parent artifact digest {digest:?}: expected 64 hex characters"
            )));
        }
        let digest = digest.to_owned();

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;
                let digest = digest.as_str();

                let mut txn = MutationTxn::begin(conn, owned.fs.journal_ctx())?;
                txn.conn().execute(
                    "INSERT INTO fs_overlay_config (key, value) VALUES (?, ?)
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (PARENT_ARTIFACT_KEY, digest.to_ascii_lowercase()),
                )?;
                txn.record(JournalDelta::overlay_config_upsert(
                    "parent_artifact",
                    PARENT_ARTIFACT_KEY,
                    &digest.to_ascii_lowercase(),
                ));
                txn.commit()?;
                Ok(())
            })
            .await
    }

    /// Read the frozen parent artifact digest, if this is a branch delta.
    pub async fn overlay_parent_artifact(&self) -> Result<Option<String>> {
        self.overlay_config_value(PARENT_ARTIFACT_KEY).await
    }

    /// Remove the parent artifact reference after its state has been folded
    /// into this database.
    pub async fn clear_overlay_parent_artifact(&self) -> Result<()> {
        self.check_background()?;

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;

                let mut txn = MutationTxn::begin(conn, owned.fs.journal_ctx())?;
                let changed = txn.conn().execute(
                    "DELETE FROM fs_overlay_config WHERE key = ?",
                    (PARENT_ARTIFACT_KEY,),
                )?;
                if changed > 0 {
                    txn.record(JournalDelta::overlay_config_delete(
                        "parent_artifact_clear",
                        PARENT_ARTIFACT_KEY,
                    ));
                }
                txn.commit()?;
                Ok(())
            })
            .await
    }

    /// Read the overlay base directory recorded at initialization, if any.
    pub async fn overlay_base_path(&self) -> Result<Option<String>> {
        self.overlay_config_value("base_path").await
    }

    async fn overlay_config_value(&self, key: &str) -> Result<Option<String>> {
        self.check_background()?;

        let key = key.to_owned();

        let owned = self.clone();
        self.pool.execute(move |conn| {
        let _keepalive = &owned;
        let key = key.as_str();

        // A database without the overlay schema is a plain Vfs.
        let mut query_statement_0 = conn.prepare_cached("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fs_overlay_config'")?;
let mut tables = query_statement_0.query([])
            ?;
        if tables.next()?.is_none() {
            return Ok(None);
        }
        let mut query_statement_1 = conn.prepare_cached("SELECT value FROM fs_overlay_config WHERE key = ?")?;
let mut rows = query_statement_1.query((key,))
            ?;
        match rows.next()? {
            Some(row) => match row.get::<_, Value>(0)? {
                Value::Text(value) => Ok(Some(value)),
                value => Err(Error::Internal(format!(
                    "invalid fs_overlay_config value for {key}: {value:?}"
                ))),
            },
            None => Ok(None),
        }
            }).await
    }

    /// Copy a consistent point-in-time image of a live database into a new
    /// single-file artifact.
    ///
    /// This is safe to call while
    /// the filesystem is serving a mount: it drains pending batched writes so
    /// every write acknowledged before the call is included, then copies a
    /// read-consistent image with `VACUUM INTO` while concurrent writers
    /// proceed. `output` must not already exist.
    pub async fn snapshot_into(&self, output: &Path) -> Result<()> {
        self.fs.drain_all().await?;
        let output = output.to_owned();
        self.pool
            .execute(move |conn| {
                vacuum_into(conn, &output)?;
                publish_single_file_artifact(&output)
            })
            .await
    }

    /// Truncate the op journal to the configured retention horizon and
    /// collect zero-refcount chunks no surviving journal entry pins.
    ///
    /// Pending batched writes are drained first so every acknowledged
    /// write is journaled before the horizon is computed.
    pub async fn collect_journal(&self) -> Result<()> {
        self.check_background()?;

        self.fs.drain_all().await?;

        let owned = self.clone();
        self.pool
            .execute(move |conn| {
                let _keepalive = &owned;

                crate::fs::journal_gc(conn, owned.fs.journal_retention_ops())?;
                // GC may have deleted zero-ref chunks the known-digest cache vouches
                // for; a stale entry would let a later commit pin a missing digest.
                owned.fs.journal_ctx().forget_chunks();
                Ok(())
            })
            .await
    }
}

fn vacuum_into(conn: &Connection, output: &Path) -> Result<()> {
    let name = output
        .to_str()
        .ok_or_else(|| Error::InvalidUtf8Path(output.display().to_string()))?;
    conn.execute("VACUUM INTO ?", [name])?;
    Ok(())
}

fn publish_single_file_artifact(output: &Path) -> Result<()> {
    let copy = Connection::open(output)?;
    checkpoint_truncate(&copy)?;
    copy.close().map_err(|(_, error)| error)?;
    remove_sidecar_if_present(output, "-wal")?;
    remove_sidecar_if_present(output, "-shm")?;
    std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(output)?
        .sync_all()?;
    Ok(())
}

fn remove_sidecar_if_present(path: &Path, suffix: &str) -> Result<()> {
    let sidecar = Path::new(&format!("{}{}", path.display(), suffix)).to_path_buf();
    match std::fs::remove_file(&sidecar) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
    }
}

pub(crate) fn checkpoint_truncate(conn: &Connection) -> Result<()> {
    let mut query_statement_0 = conn.prepare_cached("PRAGMA wal_checkpoint(TRUNCATE)")?;
    let mut rows = query_statement_0.query([])?;
    if let Some(row) = rows.next()? {
        let busy: i64 = row.get(0)?;
        if busy != 0 {
            return Err(Error::Internal(
                "WAL checkpoint could not complete because the database is busy".to_string(),
            ));
        }
    }
    while rows.next()?.is_some() {}
    Ok(())
}
