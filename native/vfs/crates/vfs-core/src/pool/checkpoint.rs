//! SQLite's threshold-based housekeeping shares the explicit checkpoint gate.
use parking_lot::Mutex;
use std::{
    cell::RefCell,
    ffi::{c_char, c_int, c_void, CStr},
};
use tokio_rusqlite::rusqlite::{ffi, Connection};

const AUTO_CHECKPOINT_PAGES: c_int = 1000;

pub(super) struct Observer<'a> {
    gate: &'a Mutex<()>,
    failure: RefCell<Option<tokio_rusqlite::rusqlite::Error>>,
}

impl<'a> Observer<'a> {
    pub(super) fn new(gate: &'a Mutex<()>) -> Self {
        Self {
            gate,
            failure: RefCell::new(None),
        }
    }

    /// The observer must outlive the registered callback. The worker removes
    /// the callback on every exit, including when the operation unwinds.
    pub(super) unsafe fn install(&self, connection: &Connection) {
        unsafe {
            ffi::sqlite3_wal_hook(
                connection.handle(),
                Some(automatic),
                std::ptr::from_ref(self).cast_mut().cast(),
            );
        }
    }

    pub(super) fn take_error(&self) -> Option<tokio_rusqlite::rusqlite::Error> {
        self.failure.take()
    }
}

unsafe extern "C" fn automatic(
    context: *mut c_void,
    connection: *mut ffi::sqlite3,
    database: *const c_char,
    pages: c_int,
) -> c_int {
    if pages < AUTO_CHECKPOINT_PAGES {
        return ffi::SQLITE_OK;
    }
    // Never wait inside a commit callback: an explicit checkpoint may be
    // waiting for this transaction to finish releasing its read lock.
    let observer = unsafe { &*context.cast::<Observer<'_>>() };
    let Some(_checkpoint) = observer.gate.try_lock() else {
        return ffi::SQLITE_OK;
    };
    // PASSIVE is the same maintenance mode SQLite uses by default. Partial
    // progress is valid; later commits retry while the WAL exceeds the limit.
    let result = unsafe {
        ffi::sqlite3_wal_checkpoint_v2(
            connection,
            database,
            ffi::SQLITE_CHECKPOINT_PASSIVE,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };
    if result != ffi::SQLITE_OK {
        // SQLite may replace this with a generic statement error while
        // propagating a WAL callback failure. Keep the checkpoint's cause.
        let message = unsafe { CStr::from_ptr(ffi::sqlite3_errmsg(connection)) }
            .to_string_lossy()
            .into_owned();
        *observer.failure.borrow_mut() = Some(tokio_rusqlite::rusqlite::Error::SqliteFailure(
            ffi::Error::new(result),
            Some(message),
        ));
    }
    result
}
