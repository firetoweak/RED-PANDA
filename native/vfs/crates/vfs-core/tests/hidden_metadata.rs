use vfs_core::{error::Result, FileSystem, Vfs, VfsOptions};

#[tokio::test]
async fn implicit_parent_copyup_preserves_directory_permissions() -> Result<()> {
    use std::sync::Arc;
    use vfs_core::OverlayFS;
    let dir = tempfile::tempdir()?;
    let base = Vfs::open(VfsOptions::ephemeral()).await?;
    let parent = FileSystem::mkdir(&base.fs, 1, "parent", 0o555, 0, 0).await?;
    let (_, file) = FileSystem::create_file(&base.fs, parent.ino, "file", 0o644, 0, 0).await?;
    file.pwrite(0, b"base").await?;
    file.fsync().await?;
    let delta = Vfs::open(VfsOptions::ephemeral()).await?;
    let overlay = OverlayFS::new(Arc::new(base.fs.clone()), delta.fs.clone());
    overlay.init(dir.path().to_str().unwrap()).await?;
    let parent = overlay.lookup(1, "parent").await?.unwrap();
    let child = overlay.lookup(parent.ino, "file").await?.unwrap();
    overlay
        .open(child.ino, libc::O_RDWR)
        .await?
        .pwrite(0, b"edit")
        .await?;
    assert_eq!(
        overlay.getattr(parent.ino).await?.unwrap().mode & 0o7777,
        0o555
    );
    assert_eq!(
        delta.fs.lookup(1, "parent").await?.unwrap().mode & 0o7777,
        0o555
    );
    Ok(())
}

#[tokio::test]
async fn hidden_is_inode_metadata_and_survives_freeze_and_reopen() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let path = dir.path().join("state.db");
    let sdk = Vfs::open(VfsOptions::with_path(path.to_string_lossy())).await?;
    let (stats, file) = FileSystem::create_file(&sdk.fs, 1, ".visible", 0o644, 0, 0).await?;
    file.pwrite(0, b"unchanged bytes").await?;
    file.fsync().await?;
    assert!(!file.fstat().await?.hidden);
    sdk.fs.set_hidden(stats.ino, true).await?;
    assert!(file.fstat().await?.hidden);
    sdk.fs.rename(1, ".visible", 1, "renamed").await?;
    assert!(sdk.fs.lookup(1, "renamed").await?.unwrap().hidden);
    let alias = sdk.fs.link(stats.ino, 1, "alias").await?;
    assert!(alias.hidden);
    let frozen_path = dir.path().join("frozen.db");
    sdk.snapshot_into(&frozen_path).await?;
    sdk.fs.set_hidden(stats.ino, false).await?;
    assert!(!sdk.fs.lookup(1, "alias").await?.unwrap().hidden);
    let frozen = Vfs::open_read_only(&frozen_path).await?;
    assert!(frozen.fs.lookup(1, "alias").await?.unwrap().hidden);
    assert!(frozen.fs.set_hidden(stats.ino, false).await.is_err());
    assert_eq!(file.pread(0, 64).await?, b"unchanged bytes");
    drop(file);
    sdk.fs.finalize().await?;
    drop(sdk);
    let reopened = Vfs::open(VfsOptions::with_path(path.to_string_lossy())).await?;
    assert!(!reopened.fs.lookup(1, "renamed").await?.unwrap().hidden);
    Ok(())
}

#[tokio::test]
async fn materializing_inherited_changes_keeps_file_and_directory_hidden_flags() -> Result<()> {
    use std::{collections::HashSet, sync::Arc};
    use vfs_core::OverlayFS;
    let dir = tempfile::tempdir()?;
    let base = Vfs::open(VfsOptions::ephemeral()).await?;
    let directory = FileSystem::mkdir(&base.fs, 1, "directory", 0o755, 0, 0).await?;
    let (stats, file) =
        FileSystem::create_file(&base.fs, directory.ino, "file", 0o644, 0, 0).await?;
    file.pwrite(0, b"content").await?;
    file.fsync().await?;
    base.fs.set_hidden(directory.ino, true).await?;
    base.fs.set_hidden(stats.ino, true).await?;
    let delta = Vfs::open(VfsOptions::ephemeral()).await?;
    let overlay = OverlayFS::new(Arc::new(base.fs.clone()), delta.fs.clone());
    overlay.init(dir.path().to_str().unwrap()).await?;
    overlay
        .materialize_base_changes(
            &HashSet::from(["/directory".into(), "/directory/file".into()]),
            &HashSet::new(),
        )
        .await?;
    let directory = delta.fs.lookup(1, "directory").await?.unwrap();
    assert!(directory.hidden);
    let stats = delta.fs.lookup(directory.ino, "file").await?.unwrap();
    assert!(stats.hidden);
    assert_eq!(
        FileSystem::open(&delta.fs, stats.ino, libc::O_RDONLY)
            .await?
            .pread(0, 64)
            .await?,
        b"content"
    );
    Ok(())
}
