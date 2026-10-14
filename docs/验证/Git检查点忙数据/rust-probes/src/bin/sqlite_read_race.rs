//! SQLite alone: two readers finish, SQLITE_TXN_NONE on every connection,
//! then checkpoint. No Vfs, pool, scheduler, WinFsp or Git.
use std::{sync::{Arc, Barrier}, time::{Duration, Instant}};
use rusqlite::{Connection, ffi};
fn open(path: &str) -> rusqlite::Result<Connection> {
    let db = Connection::open(path)?;
    db.busy_timeout(Duration::from_secs(5))?;
    db.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA wal_autocheckpoint=0;")?;
    Ok(db)
}
fn main() -> anyhow::Result<()> {
    let extended = std::env::args().any(|a| a == "--extended");
    let dir = tempfile::tempdir()?;
    let file = dir.path().join("sqlite.db");
    let path = if extended { format!(r"\\?\{}", file.display()) } else { file.display().to_string() };
    let db = open(&path)?;
    db.execute_batch("CREATE TABLE example(value INTEGER); INSERT INTO example VALUES (42)")?;
    let start = Arc::new(Barrier::new(2));
    let mut tasks = vec![];
    for _ in 0..2 {
        let path=path.clone(); let start=start.clone();
        tasks.push(std::thread::spawn(move || -> rusqlite::Result<Connection> {
            let db=open(&path)?;
            for _ in 0..10000 {
                start.wait();
                assert_eq!(db.query_row("SELECT value FROM example", [], |r| r.get::<_, i64>(0))?, 42);
            }
            Ok(db)
        }));
    }
    let readers: Vec<_> = tasks.into_iter().map(|t| t.join().unwrap().unwrap()).collect();
    let states: Vec<_> = readers.iter().chain(std::iter::once(&db)).map(|c| unsafe { ffi::sqlite3_txn_state(c.handle(), c"main".as_ptr()) }).collect();
    println!("sqlite={} extended={extended} completed_reads=20000 txn_states={states:?} autocheckpoint={}",
        rusqlite::version(), db.query_row("PRAGMA wal_autocheckpoint", [], |r| r.get::<_, i64>(0))?);
    let started=Instant::now();
    let result=db.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |r| Ok((r.get::<_, i64>(0)?,r.get::<_, i64>(1)?,r.get::<_, i64>(2)?)))?;
    println!("checkpoint={result:?} elapsed_ms={}", started.elapsed().as_millis());
    drop(readers); drop(db);
    Ok(())
}
