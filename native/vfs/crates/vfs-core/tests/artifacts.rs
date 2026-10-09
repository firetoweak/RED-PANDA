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

    // Churn writer racing the snapshot copy: every file it creates is
    // either absent from the snapshot or fully intact — never torn.
    let writer_fs = vfs.fs.clone();
    let stop = Arc::new(AtomicBool::new(false));
    let writer_stop = stop.clone();
    let writer = tokio::spawn(async move {
        let mut created = 0u32;
        while !writer_stop.load(Ordering::Relaxed) {
            let path = format!("/churn-{created}.txt");
            let (_, file) = writer_fs.create_file(&path, 0o100644, 0, 0).await?;
            file.pwrite(0, &[b'x'; 8192]).await?;
            file.fsync().await?;
            created += 1;
            // Keep the writer concurrent without starving turso's
            // VACUUM INTO I/O completion loop on the current-thread test
            // runtime.
            tokio::time::sleep(std::time::Duration::from_millis(1)).await;
        }
        Ok::<u32, Error>(created)
    });

    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    let snapshot_path = dir.path().join("snapshot.db");
    vfs.snapshot_into(&snapshot_path).await?;
    stop.store(true, Ordering::Relaxed);
    let created = writer.await.expect("writer task panicked")?;
    assert!(created > 0, "churn writer made no progress");
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
            assert_eq!(content, vec![b'x'; 8192], "torn churn file {entry}");
        }
    }
    Ok(())
}
