//! Diagnostic copy only. Keep a bounded in-memory history; print on checkpoint failure.
use std::{collections::{HashMap, VecDeque}, ffi::{c_void, CStr}, sync::{Mutex, OnceLock}, time::Instant};
use tokio_rusqlite::rusqlite::{Connection, ffi};
type ShmLock = unsafe extern "C" fn(*mut ffi::sqlite3_file, i32, i32, i32) -> i32;
static METHODS: OnceLock<Mutex<HashMap<usize, ShmLock>>> = OnceLock::new();
static EVENTS: OnceLock<Mutex<VecDeque<String>>> = OnceLock::new();
static START: OnceLock<Instant> = OnceLock::new();
pub fn record(value: String) {
    let elapsed = START.get_or_init(Instant::now).elapsed().as_micros();
    let mut events = EVENTS.get_or_init(Mutex::default).lock().unwrap();
    if events.len() == 2048 { events.pop_front(); }
    events.push_back(format!("{elapsed}us {:?} {value}", std::thread::current().id()));
}
pub fn dump() {
    eprintln!("WAL_DIAGNOSTIC_BEGIN");
    for event in EVENTS.get().unwrap().lock().unwrap().iter() { eprintln!("{event}"); }
    eprintln!("WAL_DIAGNOSTIC_END");
}
unsafe extern "C" fn shm_lock(file: *mut ffi::sqlite3_file, offset: i32, count: i32, flags: i32) -> i32 {
    let original = *METHODS.get().unwrap().lock().unwrap().get(&(file as usize)).unwrap();
    record(format!("lock request file={file:p} offset={offset} count={count} flags={flags}"));
    let started = Instant::now();
    let rc = original(file, offset, count, flags);
    record(format!("lock result file={file:p} offset={offset} count={count} flags={flags} rc={rc} elapsed={}us", started.elapsed().as_micros()));
    rc
}
unsafe extern "C" fn trace(event: u32, context: *mut c_void, statement: *mut c_void, sql: *mut c_void) -> i32 {
    if event == ffi::SQLITE_TRACE_STMT as u32 {
        let text = CStr::from_ptr(sql.cast()).to_string_lossy();
        record(format!("sql conn={context:p} stmt={statement:p} {}", text.chars().take(180).collect::<String>()));
    } else if event == ffi::SQLITE_TRACE_PROFILE as u32 {
        record(format!("sql done conn={context:p} stmt={statement:p} elapsed={}ns", *sql.cast::<u64>()));
    }
    0
}
pub unsafe fn install(db: &mut Connection) {
    let mut file: *mut ffi::sqlite3_file = std::ptr::null_mut();
    assert_eq!(ffi::sqlite3_file_control(db.handle(), c"main".as_ptr(), ffi::SQLITE_FCNTL_FILE_POINTER, (&mut file as *mut *mut ffi::sqlite3_file).cast()), ffi::SQLITE_OK);
    let original = (*file).pMethods;
    if let Some(lock) = (*original).xShmLock {
        METHODS.get_or_init(Mutex::default).lock().unwrap().insert(file as usize, lock);
        let mut methods = Box::new(*original);
        methods.xShmLock = Some(shm_lock);
        (*file).pMethods = Box::leak(methods);
    }
    record(format!("connection conn={:p} file={file:p}", db.handle()));
    ffi::sqlite3_trace_v2(db.handle(), (ffi::SQLITE_TRACE_STMT | ffi::SQLITE_TRACE_PROFILE) as u32, Some(trace), db.handle().cast());
}
