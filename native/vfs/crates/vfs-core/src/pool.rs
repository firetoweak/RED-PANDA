//! Each submitted operation owns its slot until completion is delivered or
//! abandoned. No SQLite connection or transaction escapes a worker closure.
use crate::error::{Error, Result};
use futures_util::FutureExt;
use parking_lot::Mutex;
use std::{
    any::Any,
    collections::VecDeque,
    panic::{catch_unwind, resume_unwind, AssertUnwindSafe},
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio_rusqlite::{
    rusqlite::{self, OpenFlags},
    Connection,
};

type Panic = Box<dyn Any + Send>;
enum Failure {
    Error(Error),
    Panic(Panic),
}
struct State {
    idle: Vec<Connection>,
    failures: VecDeque<Failure>,
    accepting: bool,
}
enum Source {
    Memory,
    Writable(PathBuf),
    Frozen(String),
}
struct Inner {
    source: Source,
    capacity: u32,
    slots: Arc<Semaphore>,
    state: Mutex<State>,
    checkpoint: Mutex<()>,
}

#[derive(Clone)]
pub struct ConnectionPool {
    inner: Arc<Inner>,
}
struct Completion<T> {
    result: Option<std::result::Result<Result<T>, Panic>>,
    pool: Arc<Inner>,
    _permit: OwnedSemaphorePermit,
}
impl<T> Completion<T> {
    fn observe(mut self) -> Result<T> {
        match self.result.take().unwrap() {
            Ok(result) => result,
            Err(panic) => resume_unwind(panic),
        }
    }
}
impl<T> Drop for Completion<T> {
    fn drop(&mut self) {
        let failure = match self.result.take() {
            Some(Ok(Err(error))) => Some(Failure::Error(error)),
            Some(Err(panic)) => Some(Failure::Panic(panic)),
            _ => None,
        };
        if let Some(failure) = failure {
            self.pool.state.lock().failures.push_back(failure);
        }
    }
}

impl ConnectionPool {
    fn from_source(source: Source, capacity: u32) -> Self {
        assert!(capacity > 0);
        Self {
            inner: Arc::new(Inner {
                source,
                capacity,
                slots: Arc::new(Semaphore::new(capacity as usize)),
                state: Mutex::new(State {
                    idle: Vec::new(),
                    failures: VecDeque::new(),
                    accepting: true,
                }),
                checkpoint: Mutex::new(()),
            }),
        }
    }
    pub fn writable(path: impl Into<PathBuf>, capacity: u32) -> Self {
        Self::from_source(Source::Writable(path.into()), capacity)
    }
    pub fn memory() -> Self {
        Self::from_source(Source::Memory, 1)
    }
    /// Caller supplies a frozen single-file artifact, never a live WAL family.
    pub fn frozen(path: &Path, capacity: u32) -> Result<Self> {
        if !path.is_file() {
            return Err(Error::DatabaseNotFound(path.display().to_string()));
        }
        #[cfg(windows)]
        let path = path.canonicalize()?;
        #[cfg(not(windows))]
        let path = std::path::absolute(path)?;
        let path = path
            .to_str()
            .ok_or_else(|| Error::InvalidUtf8Path(path.display().to_string()))?;
        let mut uri = String::from("file:");
        // Keep the Windows verbatim prefix for CreateFileW. A leading URI
        // slash avoids treating it as an authority; SQLite's Windows VFS
        // removes that slash after decoding the verbatim path.
        #[cfg(windows)]
        uri.push('/');
        #[cfg(not(windows))]
        if path.starts_with("//") {
            uri.push_str("//");
        }
        for byte in path.bytes() {
            if byte.is_ascii_alphanumeric() || b"/-._~:".contains(&byte) {
                uri.push(byte as char);
            } else {
                use std::fmt::Write;
                write!(uri, "%{byte:02X}").unwrap();
            }
        }
        uri.push_str("?immutable=1");
        // The default Windows VFS caps URI paths at MAX_PATH, even when
        // Rust supplied a canonical verbatim path before URI conversion.
        #[cfg(windows)]
        uri.push_str("&vfs=win32-longpath");
        Ok(Self::from_source(Source::Frozen(uri), capacity))
    }
    pub fn check_unobserved(&self) -> Result<()> {
        let failure = self.inner.state.lock().failures.pop_front();
        match failure {
            None => Ok(()),
            Some(Failure::Error(error)) => Err(error),
            Some(Failure::Panic(panic)) => resume_unwind(panic),
        }
    }
    pub(crate) fn check_ready(&self) -> Result<()> {
        self.check_unobserved()?;
        assert!(
            self.inner.state.lock().accepting,
            "database pool stopped after an internal failure"
        );
        Ok(())
    }
    pub async fn execute<T, F>(&self, operation: F) -> Result<T>
    where
        T: Send + 'static,
        F: FnOnce(&mut rusqlite::Connection) -> Result<T> + Send + 'static,
    {
        self.check_ready()?;
        let permit = tokio::time::timeout(
            Duration::from_secs(30),
            self.inner.slots.clone().acquire_owned(),
        )
        .await
        .map_err(|_| Error::ConnectionPoolTimeout)?
        .expect("pool semaphore is not closed");
        self.check_ready()?;
        let idle = self.inner.state.lock().idle.pop();
        let inner = self.inner.clone();
        if let Some(connection) = idle {
            crate::telemetry::record_connection_reuse();
            return Self::dispatch(inner, connection, permit, operation, false)
                .await
                .observe();
        }
        // Connection creation itself runs on a worker. Once admitted, a new
        // job owns the permit through open AND operation, even if its caller
        // is canceled before the open result is delivered. Existing idle
        // connections do not pay for this extra Tokio task.
        let opening = tokio::spawn(async move {
            let opened = AssertUnwindSafe(async {
                match &inner.source {
                    Source::Memory => Connection::open_in_memory().await,
                    Source::Writable(path) => Connection::open(path).await,
                    Source::Frozen(uri) => {
                        Connection::open_with_flags(
                            uri,
                            OpenFlags::SQLITE_OPEN_READ_ONLY
                                | OpenFlags::SQLITE_OPEN_URI
                                | OpenFlags::SQLITE_OPEN_NO_MUTEX,
                        )
                        .await
                    }
                }
            })
            .catch_unwind()
            .await;
            let result = match opened {
                Ok(Ok(connection)) => {
                    crate::telemetry::record_connection_create();
                    return Self::dispatch(inner, connection, permit, operation, true).await;
                }
                Ok(Err(error)) => Ok(Err(Error::Database(error))),
                Err(panic) => Err(panic),
            };
            inner.state.lock().accepting = false;
            Completion {
                result: Some(result),
                pool: inner,
                _permit: permit,
            }
        });
        match opening.await {
            Ok(completion) => completion.observe(),
            Err(error) => resume_unwind(error.into_panic()),
        }
    }
    async fn dispatch<T, F>(
        inner: Arc<Inner>,
        connection: Connection,
        permit: OwnedSemaphorePermit,
        operation: F,
        is_new: bool,
    ) -> Completion<T>
    where
        T: Send + 'static,
        F: FnOnce(&mut rusqlite::Connection) -> Result<T> + Send + 'static,
    {
        let reusable = connection.clone();
        connection
            .call_raw(move |db| {
                let result = catch_unwind(AssertUnwindSafe(|| {
                    if is_new {
                        db.busy_timeout(Duration::from_secs(5))?;
                        db.set_prepared_statement_cache_capacity(512);
                        if matches!(&inner.source, Source::Writable(_)) {
                            db.execute_batch(
                                "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
                            )?;
                        }
                    }
                    let result = operation(db);
                    if result.is_ok() {
                        assert!(
                            db.is_autocommit(),
                            "database operation leaked a transaction"
                        );
                    }
                    result
                }));
                // A legitimate filesystem rejection must not discard the only
                // in-memory database. Unknown failures stop admission and retain
                // the original error/panic in the delivery envelope.
                let healthy = db.is_autocommit()
                    && match &result {
                        Ok(Ok(_)) => true,
                        Ok(Err(error)) => expected_rejection(error),
                        Err(_) => false,
                    };
                let mut state = inner.state.lock();
                if healthy {
                    state.idle.push(reusable);
                } else {
                    crate::telemetry::record_connection_drop_discard();
                    state.accepting = false;
                }
                drop(state);
                Completion {
                    result: Some(result),
                    pool: inner,
                    _permit: permit,
                }
            })
            .await
            .expect("SQLite worker stopped outside the operation boundary")
    }
    /// Stop operation ingress before using this as a shutdown barrier.
    pub async fn barrier(&self) -> Result<()> {
        let _permits = self
            .inner
            .slots
            .clone()
            .acquire_many_owned(self.inner.capacity)
            .await
            .expect("pool semaphore is not closed");
        self.check_unobserved()
    }
    /// Terminal close after the caller has stopped ingress. Completion owns
    /// every slot and worker close, even when the awaiting caller is canceled.
    pub async fn close(&self) -> Result<()> {
        self.check_ready()?;
        let permit = self
            .inner
            .slots
            .clone()
            .acquire_many_owned(self.inner.capacity)
            .await
            .expect("pool semaphore is not closed");
        self.check_ready()?;
        let connections = {
            let mut state = self.inner.state.lock();
            state.accepting = false;
            std::mem::take(&mut state.idle)
        };
        let inner = self.inner.clone();
        let closing = tokio::spawn(async move {
            let result = AssertUnwindSafe(async move {
                for connection in connections {
                    connection.close().await.map_err(|error| match error {
                        tokio_rusqlite::Error::Close((_, error)) => Error::Database(error),
                        error => panic!("SQLite close failed outside the driver: {error:?}"),
                    })?;
                }
                Ok(())
            })
            .catch_unwind()
            .await;
            Completion {
                result: Some(result),
                pool: inner,
                _permit: permit,
            }
        });
        match closing.await {
            Ok(completion) => completion.observe(),
            Err(error) => resume_unwind(error.into_panic()),
        }
    }
    pub fn available_slots(&self) -> usize {
        self.inner.slots.available_permits()
    }
    pub fn idle_connections(&self) -> usize {
        self.inner.state.lock().idle.len()
    }

    pub(crate) fn checkpoint(&self, conn: &rusqlite::Connection) -> Result<()> {
        // SQLite does not invoke its busy handler for competing checkpointers.
        // Serialize only checkpoints; no state lock is held here or over await.
        let _checkpoint = self.inner.checkpoint.lock();
        let _timer = crate::telemetry::timer(&crate::telemetry::CORE_COUNTERS.wal_checkpoint);
        let busy: i64 = conn.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |row| row.get(0))?;
        if busy != 0 {
            return Err(rusqlite::Error::SqliteFailure(
                rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_BUSY),
                Some("WAL checkpoint is busy".into()),
            )
            .into());
        }
        Ok(())
    }
}

fn expected_rejection(error: &Error) -> bool {
    match error {
        Error::Fs(crate::fs::FsError::Corrupt(_)) => false,
        Error::Fs(_) => true,
        Error::Database(rusqlite::Error::SqliteFailure(code, _)) => matches!(
            code.code,
            rusqlite::ErrorCode::ConstraintViolation
                | rusqlite::ErrorCode::DatabaseBusy
                | rusqlite::ErrorCode::DatabaseLocked
                | rusqlite::ErrorCode::ReadOnly
        ),
        _ => false,
    }
}
