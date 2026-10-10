//! Replacing a host file with a directory must still accept children.
//! Linux openat returns ENOTDIR when alias lookup treats that host file as a directory.
use std::sync::Arc;

use tempfile::tempdir;
use vfs_core::options::VfsOptions;
use vfs_core::{FileSystem, HostFS, OverlayFS, Vfs};

#[tokio::test]
async fn directory_replacing_a_host_file_accepts_children() {
    let dir = tempdir().unwrap();
    let base_dir = dir.path().join("base");
    std::fs::create_dir(&base_dir).unwrap();
    std::fs::write(base_dir.join("a.txt"), b"AAA____AAA").unwrap();
    std::fs::create_dir(base_dir.join("keep")).unwrap();
    std::fs::write(base_dir.join("keep/note"), b"kept").unwrap();

    let base = Arc::new(HostFS::new(&base_dir).unwrap());
    let delta = Vfs::open(VfsOptions::with_path(
        dir.path().join("delta.db").to_str().unwrap(),
    ))
    .await
    .unwrap();
    let overlay = OverlayFS::new(base, delta.fs);
    overlay.init(base_dir.to_str().unwrap()).await.unwrap();

    overlay.unlink(1, "a.txt").await.unwrap();
    let directory = overlay.mkdir(1, "a.txt", 0o755, 0, 0).await.unwrap();
    assert!(directory.is_directory());
    let (child, file) = overlay
        .create_file(directory.ino, "new", 0o644, 0, 0)
        .await
        .unwrap();
    file.pwrite(0, b"new").await.unwrap();
    drop(file);
    let found = overlay.lookup(directory.ino, "new").await.unwrap().unwrap();
    assert_eq!(found.ino, child.ino);
    assert_eq!(found.size, 3);

    let keep = overlay.lookup(1, "keep").await.unwrap().unwrap();
    let note = overlay.lookup(keep.ino, "note").await.unwrap().unwrap();
    assert_eq!(note.size, 4);
}

#[tokio::test]
async fn directory_replacing_a_host_file_accepts_nested_children() {
    let dir = tempdir().unwrap();
    let base_dir = dir.path().join("base");
    std::fs::create_dir(&base_dir).unwrap();
    std::fs::write(base_dir.join("a"), b"file").unwrap();

    let base = Arc::new(HostFS::new(&base_dir).unwrap());
    let delta = Vfs::open(VfsOptions::with_path(
        dir.path().join("delta.db").to_str().unwrap(),
    ))
    .await
    .unwrap();
    let overlay = OverlayFS::new(base, delta.fs);
    overlay.init(base_dir.to_str().unwrap()).await.unwrap();

    overlay.unlink(1, "a").await.unwrap();
    let directory = overlay.mkdir(1, "a", 0o755, 0, 0).await.unwrap();
    assert!(directory.is_directory());
    let sub = overlay
        .mkdir(directory.ino, "sub", 0o755, 0, 0)
        .await
        .unwrap();
    assert!(sub.is_directory());
    let (file_stats, file) = overlay
        .create_file(sub.ino, "file", 0o644, 0, 0)
        .await
        .unwrap();
    file.pwrite(0, b"nested").await.unwrap();
    drop(file);
    let found = overlay.lookup(sub.ino, "file").await.unwrap().unwrap();
    assert_eq!(found.ino, file_stats.ino);
    assert_eq!(found.size, 6);
}

#[tokio::test]
async fn file_replacing_a_host_directory_can_be_written() {
    let dir = tempdir().unwrap();
    let base_dir = dir.path().join("base");
    std::fs::create_dir(&base_dir).unwrap();
    std::fs::create_dir(base_dir.join("node")).unwrap();
    std::fs::write(base_dir.join("node/old"), b"old").unwrap();

    let base = Arc::new(HostFS::new(&base_dir).unwrap());
    let delta = Vfs::open(VfsOptions::with_path(
        dir.path().join("delta.db").to_str().unwrap(),
    ))
    .await
    .unwrap();
    let overlay = OverlayFS::new(base, delta.fs);
    overlay.init(base_dir.to_str().unwrap()).await.unwrap();

    let node = overlay.lookup(1, "node").await.unwrap().unwrap();
    overlay.unlink(node.ino, "old").await.unwrap();
    overlay.rmdir(1, "node").await.unwrap();
    let (file_stats, file) = overlay.create_file(1, "node", 0o644, 0, 0).await.unwrap();
    file.pwrite(0, b"new").await.unwrap();
    file.truncate(3).await.unwrap();
    drop(file);
    let read = overlay.open(file_stats.ino, libc::O_RDONLY).await.unwrap();
    assert_eq!(read.pread(0, 3).await.unwrap(), b"new");
}
