//! Extended local Windows paths must not leave read locks after concurrent SQL.
#![cfg(windows)]

use std::sync::{Arc, Barrier};
use vfs_core::{config::CoreConfig, error::Result, FileSystem, Vfs, VfsOptions};

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn extended_local_path_can_checkpoint_after_parallel_readers_finish() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let root = dir
        .path()
        .canonicalize()?
        .join("检查点_本地长路径")
        .join("nested_directory_".repeat(3));
    std::fs::create_dir_all(&root)?;
    let mut config = CoreConfig::default();
    config.batcher.enabled = false;
    let sdk = Vfs::open(
        VfsOptions::with_path(root.join("vfs.db").to_string_lossy()).with_core_config(config),
    )
    .await?;
    let (stats, file) = FileSystem::create_file(&sdk.fs, 1, "file", 0o644, 0, 0).await?;
    file.pwrite(0, b"known content").await?;
    let start = Arc::new(Barrier::new(2));
    let mut readers = Vec::new();
    for _ in 0..2 {
        let pool = sdk.get_pool();
        let start = start.clone();
        let ino = stats.ino;
        readers.push(tokio::spawn(async move {
            pool.execute(move |db| {
                // Isolate read-lock release from automatic checkpoint contention.
                db.execute_batch("PRAGMA wal_autocheckpoint=0")?;
                for _ in 0..10_000 {
                    start.wait();
                    let size: i64 =
                        db.query_row("SELECT size FROM fs_inode WHERE ino=?", [ino], |row| {
                            row.get(0)
                        })?;
                    assert_eq!(size, 13);
                }
                assert!(db.is_autocommit());
                Ok(())
            })
            .await
        }));
    }
    for reader in readers {
        reader.await.unwrap()?;
    }
    sdk.get_pool().barrier().await?;
    println!("linked SQLite {}", tokio_rusqlite::rusqlite::version());
    // All readers have completed. The unchanged production barrier must now
    // sync and truncate the WAL while the pooled connections remain open.
    file.fsync().await?;
    assert_eq!(file.pread(0, 13).await?, b"known content");
    drop(file);
    sdk.fs.finalize().await?;
    sdk.get_pool().close().await?;
    Ok(())
}
