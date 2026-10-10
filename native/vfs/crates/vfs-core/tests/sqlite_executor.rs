use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::time::Duration;
use tokio_rusqlite::rusqlite;
use vfs_core::{error::Error, pool::ConnectionPool};

fn family(path: &std::path::Path) -> Vec<Option<Vec<u8>>> {
    ["", "-wal", "-shm"]
        .into_iter()
        .map(|suffix| {
            let member = std::path::PathBuf::from(format!("{}{suffix}", path.display()));
            member.exists().then(|| std::fs::read(member).unwrap())
        })
        .collect()
}

#[cfg(windows)]
#[tokio::test]
async fn canonical_windows_paths_open_the_same_immutable_artifact() {
    use vfs_core::{FileSystem, Vfs, VfsOptions};
    let directory = tempfile::tempdir().unwrap();
    let artifact = directory.path().join("冻结 # % artifact.db");
    let source = Vfs::open(VfsOptions::ephemeral()).await.unwrap();
    source.snapshot_into(&artifact).await.unwrap();
    let long_directory = directory.path().join("a".repeat(150)).join("b".repeat(150));
    std::fs::create_dir_all(&long_directory).unwrap();
    let long_artifact = long_directory.join("冻结 # % artifact.db");
    assert!(long_artifact.to_str().unwrap().len() > 300);
    std::fs::copy(&artifact, &long_artifact).unwrap();
    for artifact in [&artifact, &long_artifact] {
        let before = family(artifact);
        let canonical = artifact.canonicalize().unwrap();
        assert!(canonical.to_str().unwrap().starts_with(r"\\?\"));
        for path in [artifact, &canonical] {
            let frozen = Vfs::open_read_only(path).await.unwrap();
            assert_eq!(
                frozen.fs.file_identity(1).unwrap(),
                source.fs.file_identity(1).unwrap()
            );
            frozen.fs.finalize().await.unwrap();
        }
        assert_eq!(family(artifact), before);
    }
}
async fn actual_vfs() -> vfs_core::fs::Vfs {
    let mut config = vfs_core::config::CoreConfig::default();
    config.batcher.enabled = false;
    config.journal_enabled = true;
    vfs_core::fs::Vfs::from_pool_with_config(ConnectionPool::memory(), config)
        .await
        .unwrap()
}

#[tokio::test]
async fn cached_stats_cannot_hide_an_observed_internal_database_failure() {
    use vfs_core::fs::FileSystem;
    let fs = actual_vfs().await;
    let (_, file) = FileSystem::create_file(&fs, 1, "file", 0o644, 0, 0)
        .await
        .unwrap();
    file.fstat().await.unwrap();
    let error = fs
        .get_pool()
        .execute(|_| -> vfs_core::error::Result<()> {
            Err(rusqlite::Error::InvalidParameterName("fatal".into()).into())
        })
        .await
        .unwrap_err();
    assert!(matches!(
        error,
        Error::Database(rusqlite::Error::InvalidParameterName(_))
    ));
    assert!(tokio::spawn(async move { file.fstat().await })
        .await
        .unwrap_err()
        .is_panic());
}

#[tokio::test]
async fn bundled_sqlite_uses_the_page_cache_for_overflow_reads() {
    ConnectionPool::memory()
        .execute(|conn| {
            let enabled: bool = conn.query_row(
                "SELECT sqlite_compileoption_used('DIRECT_OVERFLOW_READ')",
                [],
                |row| row.get(0),
            )?;
            assert!(
                !enabled,
                "64 KiB chunk reads must not bypass the SQLite page cache"
            );
            Ok(())
        })
        .await
        .unwrap();
}

#[tokio::test]
async fn partial_passthrough_rejects_corrupt_inline_metadata_instead_of_reading_the_base() {
    use vfs_core::fs::{
        FileSystem, FsError, HostFS, OverlayFS, PartialOriginMode, PartialOriginPolicy,
    };
    let directory = tempfile::tempdir().unwrap();
    std::fs::write(directory.path().join("base"), vec![4; 65536]).unwrap();
    let delta = actual_vfs().await;
    let overlay = OverlayFS::new_with_partial_origin_policy(
        Arc::new(HostFS::new(directory.path()).unwrap()),
        delta.clone(),
        PartialOriginPolicy::new(PartialOriginMode::On),
    );
    overlay
        .init(directory.path().to_str().unwrap())
        .await
        .unwrap();
    let stats = overlay.lookup(1, "base").await.unwrap().unwrap();
    drop(overlay.open(stats.ino, libc::O_RDWR).await.unwrap());
    delta.get_pool().execute(|conn| {
        conn.execute("UPDATE fs_inode SET data_inline = 'invalid' WHERE ino IN (SELECT delta_ino FROM fs_partial_origin)", [])?;
        Ok(())
    }).await.unwrap();
    assert!(matches!(
        overlay.open(stats.ino, libc::O_RDONLY).await,
        Err(Error::Fs(FsError::Corrupt(_)))
    ));
}

#[tokio::test]
async fn directory_listing_preserves_a_driver_type_error_instead_of_hiding_the_entry() {
    use vfs_core::fs::FileSystem;
    let fs = actual_vfs().await;
    FileSystem::create_file(&fs, 1, "healthy", 0o644, 0, 0)
        .await
        .unwrap();
    fs.get_pool()
        .execute(|conn| {
            conn.execute(
                "UPDATE fs_dentry SET name = x'0102' WHERE name = 'healthy'",
                [],
            )?;
            Ok(())
        })
        .await
        .unwrap();
    assert!(matches!(
        FileSystem::readdir(&fs, 1).await,
        Err(Error::Database(rusqlite::Error::InvalidColumnType(_, _, _)))
    ));
}

#[tokio::test]
async fn sdk_snapshot_drains_acknowledged_bytes_and_remains_an_immutable_single_file_on_reopen() {
    use vfs_core::{FileSystem, Vfs, VfsOptions};
    let directory = tempfile::tempdir().unwrap();
    let source = Vfs::open(VfsOptions::with_path(
        directory.path().join("source.db").to_str().unwrap(),
    ))
    .await
    .unwrap();
    let (_, file) = FileSystem::create_file(&source.fs, 1, "file", 0o644, 0, 0)
        .await
        .unwrap();
    let data = vec![3; 131072];
    file.pwrite(0, &data).await.unwrap();
    let root = source.capture_root("before").await.unwrap();
    let output = directory.path().join("中文 #' snapshot.db");
    source.snapshot_into(&output).await.unwrap();
    let before = family(&output);
    let frozen = Vfs::open_read_only(&output).await.unwrap();
    let opened = frozen.fs.open("/file").await.unwrap();
    assert_eq!(opened.pread(0, 131072).await.unwrap(), data);
    assert_eq!(
        frozen.history_status().await.unwrap().head_seq,
        root.through_seq
    );
    assert!(opened.pwrite(0, b"reject").await.is_err());
    assert_eq!(opened.pread(0, 131072).await.unwrap(), data);
    frozen.fs.finalize().await.unwrap();
    assert_eq!(family(&output), before);
    let stage = directory.path().join("stage.db");
    std::fs::copy(&output, &stage).unwrap();
    Vfs::reconstruct_to(&stage, root.through_seq).await.unwrap();
    // The staging worker is explicitly closed before return, so the result
    // may immediately be reopened with immutable single-file flags.
    let replay = Vfs::open_read_only(&stage).await.unwrap();
    assert_eq!(
        replay
            .fs
            .open("/file")
            .await
            .unwrap()
            .pread(0, 131072)
            .await
            .unwrap(),
        data
    );
}

#[tokio::test]
async fn real_host_partial_copyup_preserves_base_and_never_reexposes_the_truncated_tail() {
    use vfs_core::fs::{FileSystem, HostFS, OverlayFS, PartialOriginMode, PartialOriginPolicy};
    let directory = tempfile::tempdir().unwrap();
    let original = (0..196608).map(|i| (i % 251) as u8).collect::<Vec<_>>();
    let base_path = directory.path().join("base.bin");
    std::fs::write(&base_path, &original).unwrap();
    let host = Arc::new(HostFS::new(directory.path()).unwrap());
    let delta = actual_vfs().await;
    let overlay = OverlayFS::new_with_partial_origin_policy(
        host,
        delta.clone(),
        PartialOriginPolicy::new(PartialOriginMode::On),
    );
    overlay
        .init(directory.path().to_str().unwrap())
        .await
        .unwrap();
    let stats = overlay.lookup(1, "base.bin").await.unwrap().unwrap();
    let file = overlay.open(stats.ino, libc::O_RDWR).await.unwrap();
    assert_eq!(file.pread(0, 196608).await.unwrap(), original);
    file.pwrite(65534, b"boundary").await.unwrap();
    let mut expected = original.clone();
    expected[65534..65542].copy_from_slice(b"boundary");
    assert_eq!(file.pread(0, 196608).await.unwrap(), expected);
    let overrides: i64 = delta
        .get_pool()
        .execute(|conn| {
            Ok(
                conn.query_row("SELECT count(*) FROM fs_chunk_override", [], |row| {
                    row.get(0)
                })?,
            )
        })
        .await
        .unwrap();
    assert_eq!(overrides, 2);
    file.truncate(65537).await.unwrap();
    file.truncate(196608).await.unwrap();
    expected[65537..].fill(0);
    assert_eq!(file.pread(0, 196608).await.unwrap(), expected);
    file.fsync().await.unwrap();
    assert_eq!(std::fs::read(&base_path).unwrap(), original);
}

#[tokio::test]
async fn persisted_whiteouts_hide_the_host_file_after_overlay_reload() {
    use vfs_core::fs::{FileSystem, HostFS, OverlayFS};
    let directory = tempfile::tempdir().unwrap();
    let base_path = directory.path().join("base.txt");
    std::fs::write(&base_path, b"preserved").unwrap();
    let host = Arc::new(HostFS::new(directory.path()).unwrap());
    let delta = actual_vfs().await;
    let overlay = OverlayFS::new(host.clone(), delta.clone());
    overlay
        .init(directory.path().to_str().unwrap())
        .await
        .unwrap();
    overlay.unlink(1, "base.txt").await.unwrap();
    assert!(overlay.lookup(1, "base.txt").await.unwrap().is_none());
    let reloaded = OverlayFS::new(host, delta);
    reloaded.load().await.unwrap();
    assert!(reloaded.lookup(1, "base.txt").await.unwrap().is_none());
    assert_eq!(std::fs::read(base_path).unwrap(), b"preserved");
}

#[tokio::test]
async fn namespace_replace_keeps_the_open_destination_alive_until_its_last_handle_drops() {
    use vfs_core::fs::FileSystem;
    let fs = actual_vfs().await;
    let directory = FileSystem::mkdir(&fs, 1, "directory", 0o755, 0, 0)
        .await
        .unwrap();
    let (source, source_handle) = FileSystem::create_file(&fs, 1, "source", 0o644, 0, 0)
        .await
        .unwrap();
    source_handle.pwrite(0, b"source bytes").await.unwrap();
    let (destination, destination_handle) =
        FileSystem::create_file(&fs, directory.ino, "destination", 0o644, 0, 0)
            .await
            .unwrap();
    destination_handle
        .pwrite(0, b"old destination")
        .await
        .unwrap();
    assert_eq!(
        fs.rename_with_replaced_ino(1, "source", directory.ino, "destination")
            .await
            .unwrap(),
        Some(destination.ino)
    );
    assert!(fs.lookup(1, "source").await.unwrap().is_none());
    assert_eq!(
        fs.lookup(directory.ino, "destination")
            .await
            .unwrap()
            .unwrap()
            .ino,
        source.ino
    );
    assert_eq!(
        destination_handle.pread(0, 32).await.unwrap(),
        b"old destination"
    );
    drop(destination_handle);
    fs.finalize().await.unwrap();
    assert!(fs.getattr(destination.ino).await.unwrap().is_none());
    assert_eq!(source_handle.pread(0, 32).await.unwrap(), b"source bytes");
}

#[tokio::test]
async fn import_rollback_does_not_publish_directory_ids_or_results_from_the_failed_batch() {
    use vfs_core::fs::{FileSystem, FsError, ImportEntry, ImportOptions};
    let fs = actual_vfs().await;
    let options = ImportOptions {
        uid: 42,
        gid: 43,
        timestamp: (100, 20),
    };
    let mut import = fs.begin_import(1, options).await.unwrap();
    let directory = ImportEntry {
        path: "parent".into(),
        mode: 0o040755,
        data: vec![],
    };
    let invalid = ImportEntry {
        path: "parent/..".into(),
        mode: 0o100644,
        data: vec![],
    };
    assert!(matches!(
        import.import_chunk(&[directory.clone(), invalid]).await,
        Err(Error::Fs(FsError::InvalidPath))
    ));
    assert!(fs.lookup(1, "parent").await.unwrap().is_none());
    let child = ImportEntry {
        path: "parent/child".into(),
        mode: 0o100644,
        data: b"imported".to_vec(),
    };
    assert!(matches!(
        import.import_chunk(std::slice::from_ref(&child)).await,
        Err(Error::Fs(FsError::NotFound))
    ));
    import.import_chunk(&[directory, child]).await.unwrap();
    let result = import.finish().await.unwrap();
    assert_eq!(result.len(), 2);
    assert_eq!(result[0].path, "parent");
    assert_eq!(result[1].path, "parent/child");
    let file = fs.open("/parent/child").await.unwrap();
    assert_eq!(file.pread(0, 16).await.unwrap(), b"imported");
    let stats = fs.getattr(result[1].ino).await.unwrap().unwrap();
    assert_eq!((stats.uid, stats.gid, stats.size), (42, 43, 8));
}

#[tokio::test]
async fn cancelled_transaction_retains_its_slot_until_commit_finishes() {
    let fs = actual_vfs().await;
    let pool = fs.get_pool();
    let job = pool.clone();
    let (started_tx, started_rx) = tokio::sync::oneshot::channel();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    let task = tokio::spawn(async move {
        job.execute(move |conn| {
            let txn = conn.transaction()?;
            txn.execute(
                "INSERT INTO fs_overlay_config(key,value) VALUES ('owned','committed')",
                [],
            )?;
            started_tx.send(()).unwrap();
            release_rx.recv_timeout(Duration::from_secs(10)).unwrap();
            txn.commit()?;
            Ok(())
        })
        .await
    });
    started_rx.await.unwrap();
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    assert_eq!(pool.available_slots(), 0);
    release_tx.send(()).unwrap();
    pool.barrier().await.unwrap();
    assert_eq!(pool.available_slots(), 1);
    let value: String = pool
        .execute(|conn| {
            Ok(conn.query_row(
                "SELECT value FROM fs_overlay_config WHERE key='owned'",
                [],
                |r| r.get(0),
            )?)
        })
        .await
        .unwrap();
    assert_eq!(value, "committed");
}

#[tokio::test]
async fn cancelled_unknown_failure_is_reported_by_the_barrier_with_original_type() {
    let pool = ConnectionPool::memory();
    let job = pool.clone();
    let (started_tx, started_rx) = tokio::sync::oneshot::channel();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    let task = tokio::spawn(async move {
        job.execute(move |_| -> vfs_core::error::Result<()> {
            started_tx.send(()).unwrap();
            release_rx.recv_timeout(Duration::from_secs(10)).unwrap();
            Err(Error::Database(rusqlite::Error::InvalidParameterName(
                "original".into(),
            )))
        })
        .await
    });
    started_rx.await.unwrap();
    task.abort();
    task.await.unwrap_err();
    release_tx.send(()).unwrap();
    assert!(
        matches!(pool.barrier().await, Err(Error::Database(rusqlite::Error::InvalidParameterName(name))) if name == "original")
    );
    assert_eq!(pool.available_slots(), 1);
}

#[tokio::test]
async fn cancelled_panic_preserves_the_original_payload_at_the_barrier() {
    #[derive(Debug, PartialEq)]
    struct Payload(u32);
    let pool = ConnectionPool::memory();
    let job = pool.clone();
    let (started_tx, started_rx) = tokio::sync::oneshot::channel();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    let task = tokio::spawn(async move {
        job.execute(move |_| -> vfs_core::error::Result<()> {
            started_tx.send(()).unwrap();
            release_rx.recv_timeout(Duration::from_secs(10)).unwrap();
            std::panic::panic_any(Payload(37))
        })
        .await
    });
    started_rx.await.unwrap();
    task.abort();
    task.await.unwrap_err();
    release_tx.send(()).unwrap();
    let waiter = tokio::spawn(async move { pool.barrier().await });
    assert_eq!(
        *waiter
            .await
            .unwrap_err()
            .into_panic()
            .downcast::<Payload>()
            .unwrap(),
        Payload(37)
    );
}

#[tokio::test(flavor = "current_thread")]
async fn cancellation_before_first_connection_opens_still_waits_for_the_admitted_job() {
    use std::{
        future::Future,
        task::{Context, Poll, Waker},
    };
    let pool = ConnectionPool::memory();
    let executed = Arc::new(AtomicBool::new(false));
    let flag = executed.clone();
    let mut request = Box::pin(pool.execute(move |conn| {
        conn.execute("CREATE TABLE admitted(value INTEGER)", [])?;
        flag.store(true, Ordering::SeqCst);
        Ok(())
    }));
    // On this single-thread runtime the opening task cannot run before this
    // test yields. One poll admits it, then cancellation drops its waiter.
    assert!(matches!(
        request
            .as_mut()
            .poll(&mut Context::from_waker(Waker::noop())),
        Poll::Pending
    ));
    assert!(!executed.load(Ordering::SeqCst));
    assert_eq!(pool.available_slots(), 0);
    drop(request);
    assert_eq!(pool.available_slots(), 0);
    pool.barrier().await.unwrap();
    assert!(executed.load(Ordering::SeqCst));
    pool.execute(|conn| {
        conn.execute("INSERT INTO admitted VALUES (1)", [])?;
        Ok(())
    })
    .await
    .unwrap();
}

#[tokio::test(flavor = "current_thread")]
async fn canceled_first_open_keeps_its_original_open_error_until_the_barrier() {
    use std::{
        future::Future,
        task::{Context, Poll, Waker},
    };
    let directory = tempfile::tempdir().unwrap();
    let pool = ConnectionPool::writable(directory.path().join("missing-parent/database.db"), 1);
    let mut request = Box::pin(
        pool.execute(|_| -> vfs_core::error::Result<()> { panic!("operation must not run") }),
    );
    assert!(matches!(
        request
            .as_mut()
            .poll(&mut Context::from_waker(Waker::noop())),
        Poll::Pending
    ));
    drop(request);
    assert!(
        matches!(pool.barrier().await, Err(Error::Database(rusqlite::Error::SqliteFailure(code, _))) if code.code == rusqlite::ErrorCode::CannotOpen)
    );
}
