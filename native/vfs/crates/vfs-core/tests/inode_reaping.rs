//! Inode deletion and its sidecars share the namespace transaction.
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use tempfile::tempdir;
use tokio_rusqlite::rusqlite::Connection;
use vfs_core::{
    error::{Error, Result},
    fs::{vfs::ReapHook, FsError},
    FileSystem, Vfs, VfsOptions,
};

#[derive(Default)]
struct FailOnceReapHook(AtomicBool);

#[async_trait::async_trait(?Send)]
impl ReapHook for FailOnceReapHook {
    async fn on_reap(&self, _conn: &Connection, _ino: i64) -> Result<()> {
        if !self.0.swap(true, Ordering::SeqCst) {
            return Err(FsError::PermissionDenied.into());
        }
        Ok(())
    }
}

#[tokio::test]
async fn rmdir_reap_hook_failure_rolls_back_namespace_and_origin() -> Result<()> {
    let dir = tempdir()?;
    let db = dir.path().join("delta.db");
    let sdk = Vfs::open(VfsOptions::with_path(db.to_string_lossy())).await?;
    let child = FileSystem::mkdir(&sdk.fs, 1, "refs", 0o755, 0, 0).await?;
    let identity = sdk.fs.file_identity(child.ino)?;
    let parent = sdk.fs.getattr(1).await?.unwrap();
    let conn = Connection::open(&db)?;
    conn.execute(
        "INSERT INTO fs_origin (delta_ino, base_identity) VALUES (?, ?)",
        (child.ino, &identity),
    )?;
    assert!(sdk
        .fs
        .register_reap_hook(Arc::new(FailOnceReapHook::default())));

    assert!(matches!(
        sdk.fs.rmdir(1, "refs").await,
        Err(Error::Fs(FsError::PermissionDenied))
    ));
    assert_eq!(sdk.fs.lookup(1, "refs").await?.unwrap().ino, child.ino);
    assert_eq!(sdk.fs.getattr(child.ino).await?.unwrap().nlink, child.nlink);
    assert_eq!(sdk.fs.getattr(1).await?.unwrap().nlink, parent.nlink);
    let origin: String = conn.query_row(
        "SELECT base_identity FROM fs_origin WHERE delta_ino = ?",
        (child.ino,),
        |row| row.get(0),
    )?;
    assert_eq!(origin, identity);

    sdk.fs.rmdir(1, "refs").await?;
    assert!(sdk.fs.lookup(1, "refs").await?.is_none());
    assert!(sdk.fs.getattr(child.ino).await?.is_none());
    let origins: i64 = conn.query_row("SELECT COUNT(*) FROM fs_origin", (), |row| row.get(0))?;
    assert_eq!(origins, 0);
    sdk.fs.finalize().await?;
    Ok(())
}
