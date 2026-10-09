use vfs_core::{error::Result, Vfs, VfsOptions, CURRENT};

#[tokio::test]
async fn fresh_database_contains_only_filesystem_tables() -> Result<()> {
    let vfs = Vfs::open(VfsOptions::ephemeral()).await?;
    let conn = vfs.get_connection().await?;
    let mut rows = conn
        .query("SELECT name FROM sqlite_master WHERE type = 'table'", ())
        .await?;
    while let Some(row) = rows.next().await? {
        let name = row.get::<String>(0)?;
        assert!(
            name.starts_with("fs_")
                || name == "sqlite_sequence"
                || name.starts_with("__turso_internal_"),
            "{name}"
        );
    }
    let mut rows = conn.query("PRAGMA user_version", ()).await?;
    assert_eq!(
        rows.next().await?.unwrap().get::<i64>(0)?,
        CURRENT.user_version()
    );
    Ok(())
}
