//! No mount, Git, writes, automatic checkpoints or unfinished SQL involved.
//! Compare SQLite's ordinary Windows path with the extended path used by Client.
use std::{sync::Arc, time::Instant};
use vfs_core::{Vfs, VfsOptions};
use tokio::sync::Barrier;

#[tokio::main(flavor="multi_thread", worker_threads=4)]
async fn main() -> anyhow::Result<()> {
    let extended = std::env::args().any(|a| a == "--extended");
    let dir = tempfile::tempdir()?;
    let path = dir.path().join("vfs.db");
    let path = if extended { format!(r"\\?\{}", path.display()) } else { path.display().to_string() };
    let sdk = Vfs::open(VfsOptions::with_path(&path)).await?;
    println!("sqlite={} extended={extended}", rusqlite::version());
    // Force two connections to exist, disable automatic checkpoints on both,
    // then retain them on their own workers for the controlled read workload.
    let start = Arc::new(std::sync::Barrier::new(2));
    let mut setup = vec![];
    for _ in 0..2 {
        let pool = sdk.get_pool(); let start = start.clone();
        setup.push(tokio::spawn(async move { pool.execute(move |db| {
            db.execute_batch("PRAGMA wal_autocheckpoint=0")?;
            start.wait(); Ok(())
        }).await }));
    }
    for task in setup { task.await??; }
    sdk.fs.drain_all().await?;
    // Leave a nonempty WAL so the parallel readers take shared WAL read locks.
    sdk.get_pool().execute(|db| { db.execute("UPDATE fs_inode SET size=size+1 WHERE ino=1", [])?; Ok(()) }).await?;
    let start = Arc::new(Barrier::new(2));
    let mut readers = vec![];
    for _ in 0..2 {
        let pool = sdk.get_pool(); let start = start.clone();
        readers.push(tokio::spawn(async move {
            for _ in 0..2000 {
                start.wait().await;
                pool.execute(|db| {
                    let _: i64 = db.query_row("SELECT size FROM fs_inode WHERE ino=1", [], |r| r.get(0))?;
                    Ok(())
                }).await?;
            }
            anyhow::Ok(())
        }));
    }
    for task in readers { task.await??; }
    println!("all_4000_reads_finished=true slots_available={} idle={}", sdk.get_pool().available_slots(), sdk.get_pool().idle_connections());
    let began = Instant::now(); let result = sdk.fs.drain_all().await;
    println!("checkpoint_after_all_readers={result:?} elapsed_ms={}", began.elapsed().as_millis());
    sdk.get_pool().close().await?;
    Ok(())
}
