//! Diagnostic only: pause SQLite's actual automatic checkpoint after it owns
//! the checkpoint lock, then call the unchanged production drain/checkpoint.
use std::{ffi::c_void, sync::{Arc, Condvar, Mutex, OnceLock}, time::{Duration, Instant}};
use rusqlite::ffi;
use vfs_core::{FileSystem, Vfs, VfsOptions};

type ShmLock = unsafe extern "C" fn(*mut ffi::sqlite3_file, i32, i32, i32) -> i32;
struct Gate { original: ShmLock, state: Mutex<(bool, bool)>, changed: Condvar }
static GATE: OnceLock<Arc<Gate>> = OnceLock::new();
unsafe extern "C" fn paused_checkpoint(file: *mut ffi::sqlite3_file, offset: i32, n: i32, flags: i32) -> i32 {
    let gate = GATE.get().unwrap();
    let result = (gate.original)(file, offset, n, flags);
    if result == ffi::SQLITE_OK && offset == 1 && n == 1 && flags == ffi::SQLITE_SHM_LOCK | ffi::SQLITE_SHM_EXCLUSIVE {
        let mut state = gate.state.lock().unwrap();
        if !state.0 {
            state.0 = true;
            gate.changed.notify_all();
            let (next, timeout) = gate.changed.wait_timeout_while(state, Duration::from_secs(20), |s| !s.1).unwrap();
            assert!(!timeout.timed_out(), "diagnostic release timed out: {next:?}");
        }
    }
    result
}

#[tokio::main(flavor = "multi_thread", worker_threads = 4)]
async fn main() -> anyhow::Result<()> {
    let options: Vec<_> = std::env::args().collect();
    let disabled = options.iter().any(|a| a == "--off");
    let file_sync = options.iter().any(|a| a == "--file");
    let dir = tempfile::tempdir()?;
    let sdk = Vfs::open(VfsOptions::with_path(dir.path().join("vfs.db").to_string_lossy())).await?;
    let (_, file) = FileSystem::create_file(&sdk.fs, 1, "file", 0o644, 0, 0).await?;
    let pool = sdk.get_pool();
    pool.execute(|db| {
        db.execute_batch("CREATE TABLE diagnostic(value BLOB)")?;
        println!("sqlite={} autocheckpoint={} busy_timeout={}", rusqlite::version(),
            db.query_row("PRAGMA wal_autocheckpoint", [], |r| r.get::<_, i64>(0))?,
            db.query_row("PRAGMA busy_timeout", [], |r| r.get::<_, i64>(0))?);
        Ok(())
    }).await?;
    let writer = tokio::spawn(async move {
        pool.execute(move |db| {
            if disabled { db.execute_batch("PRAGMA wal_autocheckpoint=0")?; }
            println!("writer_autocheckpoint={}", db.query_row("PRAGMA wal_autocheckpoint", [], |r| r.get::<_, i64>(0))?);
            unsafe {
                let mut file: *mut ffi::sqlite3_file = std::ptr::null_mut();
                assert_eq!(ffi::sqlite3_file_control(db.handle(), c"main".as_ptr(), ffi::SQLITE_FCNTL_FILE_POINTER,
                    &mut file as *mut _ as *mut c_void), ffi::SQLITE_OK);
                let original = (*file).pMethods;
                let gate = Arc::new(Gate { original: (*original).xShmLock.unwrap(), state: Mutex::new((false, false)), changed: Condvar::new() });
                assert!(GATE.set(gate).is_ok());
                let mut methods = Box::new(*original);
                methods.xShmLock = Some(paused_checkpoint);
                (*file).pMethods = &*methods;
                // More than the real default 1000 pages; no manual checkpoint
                // call or changed auto-checkpoint threshold on this connection.
                let result = db.execute("INSERT INTO diagnostic VALUES (zeroblob(8388608))", []);
                (*file).pMethods = original;
                result?;
            }
            Ok(())
        }).await
    });
    let gate = loop {
        if let Some(gate) = GATE.get() { break gate.clone(); }
        tokio::task::yield_now().await;
    };
    if disabled {
        writer.await??;
        let result = if file_sync { file.fsync().await } else { sdk.fs.drain_all().await };
        println!("auto_disabled_production_result={result:?}");
        assert!(result.is_ok());
        println!("automatic_checkpoint_started={}", gate.state.lock().unwrap().0);
        drop(file);
        sdk.fs.finalize().await?;
        sdk.get_pool().close().await?;
        return Ok(());
    }
    let waiting = gate.clone();
    tokio::task::spawn_blocking(move || {
        let state = waiting.state.lock().unwrap();
        let (_state, timeout) = waiting.changed.wait_timeout_while(state, Duration::from_secs(10), |s| !s.0).unwrap();
        assert!(!timeout.timed_out(), "automatic checkpoint did not start");
    }).await?;
    let started = Instant::now();
    let result = if file_sync { file.fsync().await } else { sdk.fs.drain_all().await };
    println!("production_result={result:?} file_sync={file_sync} elapsed_us={}", started.elapsed().as_micros());
    let mode = sdk.get_pool().execute(|db| Ok(db.query_row("PRAGMA synchronous", [], |r| r.get::<_, i64>(0))?)).await?;
    println!("reused_connection_synchronous_after_failure={mode}");
    {
        let mut state = gate.state.lock().unwrap(); state.1 = true;
        gate.changed.notify_all();
    }
    writer.await??;
    let count = sdk.get_pool().execute(|db| Ok(db.query_row("SELECT length(value) FROM diagnostic", [], |r| r.get::<_, i64>(0))?)).await?;
    println!("committed_bytes={count}");
    assert!(format!("{result:?}").contains("WAL checkpoint is busy"));
    drop(file);
    sdk.fs.finalize().await?;
    sdk.get_pool().close().await?;
    Ok(())
}
