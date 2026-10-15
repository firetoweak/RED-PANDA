use std::path::Path;
use tempfile::tempdir;
use vfs_core::error::{Error, Result};
use vfs_core::{Vfs, VfsOptions};

#[tokio::test]
async fn snapshot_into_is_consistent_under_concurrent_writes() -> Result<()> {
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    let dir = tempdir()?;
    let db_path = dir.path().join("session.db");
    let vfs = Vfs::open(VfsOptions::with_path(db_path.to_string_lossy())).await?;

    let (_, file) = vfs.fs.create_file("/pinned.txt", 0o100644, 0, 0).await?;
    file.pwrite(0, b"pinned before snapshot").await?;
    file.fsync().await?;
    drop(file);

    // Creation and the first write are separate operations: a snapshot between
    // them may legitimately contain an empty file. Seed the files first so
    // this race tests atomic content writes without assuming atomic creation.
    for index in 0..4 {
        let (_, file) = vfs
            .fs
            .create_file(&format!("/churn-{index}.txt"), 0o100644, 0, 0)
            .await?;
        file.pwrite(0, &[b'x'; 8192]).await?;
        file.fsync().await?;
    }
    let writer_fs = vfs.fs.clone();
    let stop = Arc::new(AtomicBool::new(false));
    let writer_stop = stop.clone();
    let (started, ready) = tokio::sync::oneshot::channel();
    let writer = tokio::spawn(async move {
        let mut writes = 0u32;
        let mut started = Some(started);
        while !writer_stop.load(Ordering::Relaxed) {
            let path = format!("/churn-{}.txt", writes % 4);
            let file = writer_fs.open(&path).await?;
            let byte = b'y' + ((writes / 4) % 2) as u8;
            file.pwrite(0, &[byte; 8192]).await?;
            file.fsync().await?;
            writes += 1;
            if let Some(started) = started.take() {
                started.send(()).unwrap();
            }
            // Allow the snapshot task to race repeated committed writes.
            tokio::time::sleep(std::time::Duration::from_millis(1)).await;
        }
        Ok::<u32, Error>(writes)
    });

    ready.await.expect("writer stopped before its first write");
    let snapshot_path = dir.path().join("snapshot.db");
    vfs.snapshot_into(&snapshot_path).await?;
    stop.store(true, Ordering::Relaxed);
    let writes = writer.await.expect("writer task panicked")?;
    assert!(writes > 0, "churn writer made no progress");
    drop(vfs);

    for suffix in ["-wal", "-shm"] {
        let sidecar = format!("{}{suffix}", snapshot_path.display());
        assert!(
            !Path::new(&sidecar).exists(),
            "snapshot left sidecar {sidecar}"
        );
    }

    let snapshot = Vfs::open(VfsOptions::with_path(snapshot_path.to_string_lossy())).await?;
    assert_eq!(
        snapshot.fs.read_file("/pinned.txt").await?.as_deref(),
        Some(b"pinned before snapshot".as_slice())
    );
    let entries = vfs_core::fs::FileSystem::readdir(&snapshot.fs, 1)
        .await?
        .expect("snapshot root must list");
    for entry in entries {
        if let Some(rest) = entry.strip_prefix("churn-") {
            let content = snapshot
                .fs
                .read_file(&format!("/{entry}"))
                .await?
                .unwrap_or_else(|| panic!("churn file {rest} listed but unreadable"));
            assert_eq!(content.len(), 8192, "truncated churn file {entry}");
            let byte = content[0];
            assert!(matches!(byte, b'x' | b'y' | b'z'));
            assert!(
                content.iter().all(|value| *value == byte),
                "torn churn file {entry}"
            );
        }
    }
    Ok(())
}
