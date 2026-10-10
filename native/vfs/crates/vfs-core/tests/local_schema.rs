use vfs_core::{error::Result, Vfs, VfsOptions, CURRENT};

#[tokio::test]
async fn fresh_database_contains_only_filesystem_tables() -> Result<()> {
    let vfs = Vfs::open(VfsOptions::ephemeral()).await?;
    vfs.get_pool()
        .execute(move |conn| {
            let mut statement_0 =
                conn.prepare("SELECT name FROM sqlite_master WHERE type = 'table'")?;
            let mut rows = statement_0.query(())?;
            while let Some(row) = rows.next()? {
                let name = row.get::<_, String>(0)?;
                assert!(
                    name.starts_with("fs_") || name == "sqlite_sequence",
                    "{name}"
                );
            }
            let mut statement_1 = conn.prepare("PRAGMA user_version")?;
            let mut rows = statement_1.query(())?;
            assert_eq!(
                rows.next()?.unwrap().get::<_, i64>(0)?,
                CURRENT.user_version()
            );
            Ok(())
        })
        .await
}
