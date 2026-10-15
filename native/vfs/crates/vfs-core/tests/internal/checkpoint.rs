//! Checkpoint coordination and temporary synchronous settings are pool contracts.
use super::*;
use parking_lot::Condvar;
use rusqlite::{
    ffi,
    hooks::{AuthAction, Authorization},
    Connection as Sqlite,
};
use std::{
    cell::RefCell,
    ffi::c_void,
    sync::atomic::{AtomicUsize, Ordering},
};

type ShmLock = unsafe extern "C" fn(*mut ffi::sqlite3_file, i32, i32, i32) -> i32;

#[derive(Default)]
struct Probe {
    calls: AtomicUsize,
    pause: bool,
    state: Mutex<(bool, bool)>,
    changed: Condvar,
    failure: Option<i32>,
}

// A test-only SQLite I/O interception. Each pooled connection has a dedicated
// worker thread; the override is scoped to one worker job and restored on drop.
thread_local! {
    static IO: RefCell<Option<(ShmLock, Arc<Probe>)>> = const { RefCell::new(None) };
}

unsafe extern "C" fn shm_lock(
    file: *mut ffi::sqlite3_file,
    offset: i32,
    count: i32,
    flags: i32,
) -> i32 {
    IO.with_borrow(|io| {
        let (original, probe) = io.as_ref().unwrap();
        let checkpoint =
            offset == 1 && count == 1 && flags == ffi::SQLITE_SHM_LOCK | ffi::SQLITE_SHM_EXCLUSIVE;
        if checkpoint {
            probe.calls.fetch_add(1, Ordering::SeqCst);
            if let Some(failure) = probe.failure {
                return failure;
            }
        }
        let result = unsafe { original(file, offset, count, flags) };
        if checkpoint && result == ffi::SQLITE_OK && probe.pause {
            let mut state = probe.state.lock();
            state.0 = true;
            probe.changed.notify_all();
            while !state.1 {
                if probe
                    .changed
                    .wait_for(&mut state, Duration::from_secs(10))
                    .timed_out()
                    && !state.1
                {
                    return ffi::SQLITE_IOERR_SHMLOCK;
                }
            }
        }
        result
    })
}

struct IoOverride {
    file: *mut ffi::sqlite3_file,
    original: *const ffi::sqlite3_io_methods,
    _methods: Box<ffi::sqlite3_io_methods>,
}
impl IoOverride {
    fn install(db: &Sqlite, probe: Arc<Probe>) -> Self {
        unsafe {
            let mut file: *mut ffi::sqlite3_file = std::ptr::null_mut();
            let result = ffi::sqlite3_file_control(
                db.handle(),
                c"main".as_ptr(),
                ffi::SQLITE_FCNTL_FILE_POINTER,
                std::ptr::from_mut(&mut file).cast::<c_void>(),
            );
            assert_eq!(result, ffi::SQLITE_OK);
            let original = (*file).pMethods;
            let mut methods = Box::new(*original);
            IO.with_borrow_mut(|io| *io = Some((methods.xShmLock.unwrap(), probe)));
            methods.xShmLock = Some(shm_lock);
            (*file).pMethods = &*methods;
            Self {
                file,
                original,
                _methods: methods,
            }
        }
    }
}
impl Drop for IoOverride {
    fn drop(&mut self) {
        unsafe { (*self.file).pMethods = self.original };
        IO.with_borrow_mut(|io| *io = None);
    }
}

async fn database(capacity: u32) -> (ConnectionPool, tempfile::TempDir) {
    let directory = tempfile::tempdir().unwrap();
    let pool = ConnectionPool::writable(directory.path().join("checkpoint.db"), capacity);
    pool.execute(|db| {
        db.execute_batch("CREATE TABLE data(value BLOB); INSERT INTO data VALUES (x'01')")?;
        Ok(())
    })
    .await
    .unwrap();
    (pool, directory)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn automatic_checkpoint_yields_to_explicit_gate_and_reclaims_on_next_commit() {
    let (pool, _directory) = database(1).await;
    let locked_pool = pool.clone();
    let (locked, ready) = tokio::sync::oneshot::channel();
    let (release, wait) = std::sync::mpsc::channel();
    let holder = tokio::task::spawn_blocking(move || {
        let _guard = locked_pool.inner.checkpoint.lock();
        locked.send(()).unwrap();
        let _ = wait.recv_timeout(Duration::from_secs(10));
    });
    ready.await.unwrap();
    let probe = Arc::new(Probe::default());
    let observed = probe.clone();
    let write = pool.execute(move |db| {
        let _io = IoOverride::install(db, observed);
        // Over the real 1000-page threshold, with the default page geometry.
        db.execute("INSERT INTO data VALUES (zeroblob(8388608))", [])?;
        Ok(())
    });
    tokio::time::timeout(Duration::from_secs(5), write)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        probe.calls.load(Ordering::SeqCst),
        0,
        "automatic maintenance must not compete with the explicit gate"
    );
    release.send(()).unwrap();
    holder.await.unwrap();
    let observed = probe.clone();
    pool.execute(move |db| {
        let _io = IoOverride::install(db, observed);
        db.execute("INSERT INTO data VALUES (x'02')", [])?;
        assert!(db.is_autocommit());
        let (frames, copied): (i64, i64) =
            db.query_row("PRAGMA wal_checkpoint(NOOP)", [], |row| {
                Ok((row.get(1)?, row.get(2)?))
            })?;
        assert!(frames > 1000);
        assert_eq!(
            frames, copied,
            "threshold housekeeping must reclaim the accumulated log"
        );
        Ok(())
    })
    .await
    .unwrap();
    assert!(probe.calls.load(Ordering::SeqCst) > 0);
    pool.close().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn explicit_sync_waits_for_an_active_automatic_checkpoint() {
    let (pool, _directory) = database(2).await;
    let probe = Arc::new(Probe {
        pause: true,
        ..Probe::default()
    });
    let observed = probe.clone();
    let writing_pool = pool.clone();
    let writer = tokio::spawn(async move {
        writing_pool
            .execute(move |db| {
                let _io = IoOverride::install(db, observed);
                db.execute("INSERT INTO data VALUES (zeroblob(8388608))", [])?;
                Ok(())
            })
            .await
    });
    let observed = probe.clone();
    tokio::task::spawn_blocking(move || {
        let mut state = observed.state.lock();
        while !state.0 {
            assert!(!observed
                .changed
                .wait_for(&mut state, Duration::from_secs(5))
                .timed_out());
        }
    })
    .await
    .unwrap();
    assert!(
        pool.inner.checkpoint.try_lock().is_none(),
        "automatic maintenance must own the explicit checkpoint gate"
    );
    let (started, ready) = tokio::sync::oneshot::channel();
    pool.execute(move |db| {
        let mut started = Some(started);
        db.authorizer(Some(move |context: rusqlite::hooks::AuthContext<'_>| {
            if matches!(
                context.action,
                AuthAction::Pragma {
                    pragma_name: "synchronous",
                    pragma_value: Some("FULL")
                }
            ) {
                started.take().unwrap().send(()).unwrap();
            }
            Authorization::Allow
        }))?;
        Ok(())
    })
    .await
    .unwrap();
    let syncing_pool = pool.clone();
    let sync = tokio::spawn(async move {
        let operation_pool = syncing_pool.clone();
        syncing_pool
            .execute(move |db| operation_pool.sync(db))
            .await
    });
    tokio::time::timeout(Duration::from_secs(5), ready)
        .await
        .unwrap()
        .unwrap();
    assert!(!sync.is_finished());
    probe.state.lock().1 = true;
    probe.changed.notify_all();
    writer.await.unwrap().unwrap();
    sync.await.unwrap().unwrap();
    pool.close().await.unwrap();
}

#[tokio::test]
async fn failed_sync_restores_normal_before_reusing_the_connection() {
    let (pool, directory) = database(1).await;
    let reader = Sqlite::open(directory.path().join("checkpoint.db")).unwrap();
    reader.execute_batch("BEGIN; SELECT * FROM data").unwrap();
    let operation_pool = pool.clone();
    let error = pool
        .execute(move |db| {
            db.busy_timeout(Duration::ZERO)?;
            operation_pool.sync(db)
        })
        .await
        .unwrap_err();
    assert!(
        matches!(error, Error::Database(rusqlite::Error::SqliteFailure(code, _))
        if code.code == rusqlite::ErrorCode::DatabaseBusy)
    );
    let mode: i64 = pool
        .execute(|db| Ok(db.query_row("PRAGMA synchronous", [], |row| row.get(0))?))
        .await
        .unwrap();
    assert_eq!(mode, 1);
    reader.execute_batch("ROLLBACK").unwrap();
    pool.close().await.unwrap();
}

#[tokio::test]
async fn failed_setting_restoration_preserves_both_errors_and_stops_reuse() {
    for reject_checkpoint in [false, true] {
        let (pool, _directory) = database(1).await;
        let operation_pool = pool.clone();
        let error = pool
            .execute(move |db| {
                db.authorizer(Some(
                    move |context: rusqlite::hooks::AuthContext<'_>| match context.action {
                        AuthAction::Pragma {
                            pragma_name: "synchronous",
                            pragma_value: Some("NORMAL"),
                        } => Authorization::Deny,
                        AuthAction::Pragma {
                            pragma_name: "wal_checkpoint",
                            ..
                        } if reject_checkpoint => Authorization::Deny,
                        _ => Authorization::Allow,
                    },
                ))?;
                operation_pool.sync(db)
            })
            .await
            .unwrap_err();
        let Error::SynchronousRestore {
            checkpoint,
            restore,
        } = error
        else {
            panic!("expected restoration failure, got {error:?}");
        };
        assert_eq!(checkpoint.is_some(), reject_checkpoint);
        assert_eq!(
            restore.sqlite_error_code(),
            Some(rusqlite::ErrorCode::AuthorizationForStatementDenied)
        );
        if let Some(checkpoint) = checkpoint {
            assert!(
                matches!(*checkpoint, Error::Database(rusqlite::Error::SqliteFailure(code, _))
                if code.code == rusqlite::ErrorCode::AuthorizationForStatementDenied)
            );
        }
        assert_eq!(pool.idle_connections(), 0);
        assert!(tokio::spawn(async move { pool.execute(|_| Ok(())).await })
            .await
            .unwrap_err()
            .is_panic());
    }
}

#[tokio::test]
async fn automatic_checkpoint_io_failure_is_not_hidden_by_a_successful_commit() {
    let (pool, _directory) = database(1).await;
    let probe = Arc::new(Probe {
        failure: Some(ffi::SQLITE_IOERR_SHMLOCK),
        ..Probe::default()
    });
    let error = pool
        .execute(move |db| {
            let _io = IoOverride::install(db, probe);
            db.execute("INSERT INTO data VALUES (zeroblob(8388608))", [])?;
            Ok(())
        })
        .await
        .unwrap_err();
    assert!(
        matches!(&error, Error::AutomaticCheckpoint { checkpoint: rusqlite::Error::SqliteFailure(code, _), .. }
        if code.extended_code == ffi::SQLITE_IOERR_SHMLOCK),
        "{error:?}"
    );
    assert_eq!(pool.idle_connections(), 0);
}
