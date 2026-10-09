//! Current-format validation; older origin identities cannot be migrated reliably.
use super::safety::{build_local_database, ReadOnlyOpenSidecars};
use anyhow::{Context, Result};
use std::{
    io::Write,
    path::{Path, PathBuf},
};
use vfs_core::{error::Error as SdkError, schema, SchemaVersion, VfsOptions};

fn schema_upgrade_guidance(found: &str, expected: &str, id_or_path: &str) -> String {
    match SchemaVersion::parse(found) {
        Some(version) if version<schema::MIN_SUPPORTED=>format!(
            "Filesystem `{id_or_path}` uses unsupported schema {found}. Old origin identities lack persistent physical or delta ownership information. This fork requires {expected}; the original database remains unchanged; automatic migration is unavailable."
        ),
        _=>format!("Filesystem `{id_or_path}` has unsupported schema version {found}; this fork requires {expected}."),
    }
}
pub(crate) fn open_error_with_guidance(err: SdkError, id_or_path: &str) -> anyhow::Error {
    match &err {
        SdkError::SchemaVersionMismatch { found, expected } => {
            let message = schema_upgrade_guidance(found, expected, id_or_path);
            anyhow::Error::from(err).context(message)
        }
        _ => err.into(),
    }
}

pub async fn handle_migrate_command(
    stdout: &mut impl Write,
    id_or_path: String,
    dry_run: bool,
    encryption: Option<&(String, String)>,
) -> Result<()> {
    let path = VfsOptions::resolve(&id_or_path)?.db_path()?;
    let path = Path::new(&path);
    let sidecars = ReadOnlyOpenSidecars::capture(path);
    let result = async {
        let db = build_local_database(path, encryption).await?;
        let conn = db.connect().context("Failed to connect to database")?;
        let found = schema::detect_schema_version(&conn).await?;
        if found != Some(schema::CURRENT) {
            return Err(open_error_with_guidance(
                SdkError::SchemaVersionMismatch {
                    found: found
                        .map(|v| v.to_string())
                        .unwrap_or_else(|| "uninitialized".into()),
                    expected: schema::CURRENT.to_string(),
                },
                &id_or_path,
            ));
        }
        if dry_run {
            schema::check_schema_version(&conn).await?;
        } else {
            schema::ensure_current(&conn).await?;
            let mut rows = conn.query("PRAGMA wal_checkpoint(TRUNCATE)", ()).await?;
            while rows.next().await?.is_some() {}
        }
        writeln!(stdout, "Database is already at schema {}.", schema::CURRENT)?;
        Ok(())
    }
    .await;
    sidecars.remove_created_frameless();
    result
}

pub async fn handle_migrate_copy_command(
    _stdout: &mut impl Write,
    _id_or_path: String,
    _target: PathBuf,
    _verify: bool,
    _overwrite_target: bool,
    _encryption: Option<&(String, String)>,
) -> Result<()> {
    anyhow::bail!("Schema {} has no copy-migration path; older origin ownership cannot be recovered. Use vfs backup for a current database.", schema::CURRENT)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn old_format_guidance_does_not_offer_an_impossible_migration() {
        for version in ["0.0", "0.2", "0.4", "0.5", "0.6", "0.7", "0.8", "0.9"] {
            let message = schema_upgrade_guidance(version, schema::CURRENT.as_str(), "example");
            assert!(message.contains("automatic migration is unavailable"));
            assert!(!message.contains("To upgrade, run"));
        }
    }
}
