//! Immutable artifacts, overlay lineage and history maintenance.

use std::path::Path;

use turso::{Builder, Connection, Value};

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
        if digest.len() != 64 || !digest.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(Error::Internal(format!(
                "invalid parent artifact digest {digest:?}: expected 64 hex characters"
            )));
        }
        let conn = self.pool.get_connection().await?;
        let mut txn = MutationTxn::begin(&conn, self.fs.journal_ctx()).await?;
        txn.conn()
            .execute(
                "INSERT INTO fs_overlay_config (key, value) VALUES (?, ?)
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (PARENT_ARTIFACT_KEY, digest.to_ascii_lowercase()),
            )
            .await?;
        txn.record(JournalDelta::overlay_config_upsert(
            "parent_artifact",
            PARENT_ARTIFACT_KEY,
            &digest.to_ascii_lowercase(),
        ));
        txn.commit().await?;
        Ok(())
    }

    /// Read the frozen parent artifact digest, if this is a branch delta.
    pub async fn overlay_parent_artifact(&self) -> Result<Option<String>> {
        self.overlay_config_value(PARENT_ARTIFACT_KEY).await
    }

    /// Remove the parent artifact reference after its state has been folded
    /// into this database.
    pub async fn clear_overlay_parent_artifact(&self) -> Result<()> {
        let conn = self.pool.get_connection().await?;
        let mut txn = MutationTxn::begin(&conn, self.fs.journal_ctx()).await?;
        let changed = txn
            .conn()
            .execute(
                "DELETE FROM fs_overlay_config WHERE key = ?",
                (PARENT_ARTIFACT_KEY,),
            )
            .await?;
        if changed > 0 {
            txn.record(JournalDelta::overlay_config_delete(
                "parent_artifact_clear",
                PARENT_ARTIFACT_KEY,
            ));
        }
        txn.commit().await?;
        Ok(())
    }

    /// Read the overlay base directory recorded at initialization, if any.
    pub async fn overlay_base_path(&self) -> Result<Option<String>> {
        self.overlay_config_value("base_path").await
    }

    async fn overlay_config_value(&self, key: &str) -> Result<Option<String>> {
        let conn = self.pool.get_connection().await?;
        // A database without the overlay schema is a plain Vfs.
        let mut tables = conn
            .query(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fs_overlay_config'",
                (),
            )
            .await?;
        if tables.next().await?.is_none() {
            return Ok(None);
        }
        let mut rows = conn
            .query("SELECT value FROM fs_overlay_config WHERE key = ?", (key,))
            .await?;
        match rows.next().await? {
            Some(row) => match row.get_value(0)? {
                Value::Text(value) => Ok(Some(value)),
                value => Err(Error::Internal(format!(
                    "invalid fs_overlay_config value for {key}: {value:?}"
                ))),
            },
            None => Ok(None),
        }
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
        let conn = self.pool.get_connection().await?;
        vacuum_into(&conn, output).await?;
        drop(conn);
        publish_single_file_artifact(output).await
    }

    /// Truncate the op journal to the configured retention horizon and
    /// collect zero-refcount chunks no surviving journal entry pins.
    ///
    /// Pending batched writes are drained first so every acknowledged
    /// write is journaled before the horizon is computed.
    pub async fn collect_journal(&self) -> Result<()> {
        self.fs.drain_all().await?;
        let conn = self.pool.get_connection().await?;
        crate::fs::journal_gc(&conn, self.fs.journal_retention_ops()).await?;
        // GC may have deleted zero-ref chunks the known-digest cache vouches
        // for; a stale entry would let a later commit pin a missing digest.
        self.fs.journal_ctx().forget_chunks();
        Ok(())
    }
}

async fn vacuum_into(conn: &Connection, output: &Path) -> Result<()> {
    let escaped_output = output.to_string_lossy().replace('\'', "''");
    conn.execute(&format!("VACUUM INTO '{escaped_output}'"), ())
        .await?;
    Ok(())
}

/// Checkpoint a freshly written copy into a durable single-file family.
async fn publish_single_file_artifact(output: &Path) -> Result<()> {
    let output_str = output
        .to_str()
        .ok_or_else(|| Error::InvalidUtf8Path(output.display().to_string()))?;
    let output_db = Builder::new_local(output_str).build().await?;
    let output_conn = output_db.connect()?;
    checkpoint_truncate(&output_conn).await?;
    drop(output_conn);
    drop(output_db);
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

async fn checkpoint_truncate(conn: &Connection) -> Result<()> {
    let mut rows = conn.query("PRAGMA wal_checkpoint(TRUNCATE)", ()).await?;
    if let Some(row) = rows.next().await? {
        let busy: i64 = row.get(0)?;
        if busy != 0 {
            return Err(Error::Internal(
                "WAL checkpoint could not complete because the database is busy".to_string(),
            ));
        }
    }
    while rows.next().await?.is_some() {}
    Ok(())
}
