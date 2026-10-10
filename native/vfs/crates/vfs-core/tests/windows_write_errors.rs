//! Failed COW reads and SQL mutations leave the committed file state intact.
#![cfg(windows)]
use std::{
    path::Path,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
};
use tokio_rusqlite::rusqlite::Connection;
use vfs_core::{
    error::{Error, Result},
    fs::BaseValidator,
    schema::integrity::{check, CheckOpts},
    FileSystem, HostFS, OverlayFS, PartialOriginMode, PartialOriginPolicy, Stats, Vfs, VfsOptions,
    WriteRange,
};

struct ReadFailure(AtomicUsize);
impl BaseValidator for ReadFailure {
    fn validate(&self, _: &dyn FileSystem, _: &Stats) -> Result<()> {
        if self.0.fetch_add(1, Ordering::SeqCst) == 1 {
            return Err(std::io::Error::from_raw_os_error(1117).into());
        }
        Ok(())
    }
}
async fn summary(sdk: &Vfs) -> Result<Vec<i64>> {
    sdk.get_pool()
        .execute(|conn| {
            let mut counts = Vec::new();
            for table in ["fs_data", "fs_chunk", "fs_chunk_override"] {
                let mut statement_0 = conn.prepare(&format!("SELECT COUNT(*) FROM {table}"))?;
                let mut rows = statement_0.query(())?;
                counts.push(rows.next()?.unwrap().get(0)?);
            }
            Ok(counts)
        })
        .await
}
async fn view(base: &Path, sdk: &Vfs, fault: Arc<ReadFailure>) -> Result<OverlayFS> {
    let fs = OverlayFS::new_with_partial_origin_policy(
        Arc::new(HostFS::new(base)?),
        sdk.fs.clone(),
        PartialOriginPolicy::new(PartialOriginMode::On),
    )
    .with_base_validator(fault);
    fs.init(base.to_str().unwrap()).await?;
    Ok(fs)
}
fn changes() -> Vec<WriteRange> {
    vec![
        WriteRange {
            offset: 7,
            data: b"XYZ".to_vec(),
        },
        WriteRange {
            offset: 65543,
            data: b"RST".to_vec(),
        },
    ]
}

#[tokio::test]
async fn failed_multichunk_write_preserves_data_and_metadata() -> Result<()> {
    for sql_failure in [false, true] {
        let dir = tempfile::tempdir()?;
        let base = dir.path().join("base");
        std::fs::create_dir(&base)?;
        let original = vec![b'A'; 131072];
        std::fs::write(base.join("data.bin"), &original)?;
        std::fs::hard_link(base.join("data.bin"), base.join("alias.bin"))?;
        let db = dir.path().join("delta.db");
        let sdk = Vfs::open(VfsOptions::with_path(db.to_string_lossy())).await?;
        let fault = Arc::new(ReadFailure(AtomicUsize::new(100)));
        let fs = view(&base, &sdk, fault.clone()).await?;
        let stats = fs.lookup(1, "data.bin").await?.unwrap();
        let file = fs.open(stats.ino, libc::O_RDWR).await?;
        file.fsync().await?;
        let before = file.fstat().await?;
        let counts = summary(&sdk).await?;
        let conn = Connection::open(&db)?;
        if sql_failure {
            // Fail after the first mapping was inserted in this transaction.
            // This trigger exists only in this isolated test database.
            conn.execute(
                "CREATE TRIGGER injected_second_chunk BEFORE INSERT ON fs_data
                 WHEN NEW.chunk_index = 1 AND EXISTS
                   (SELECT 1 FROM fs_data WHERE ino = NEW.ino AND chunk_index = 0)
                 BEGIN SELECT RAISE(ABORT, 'injected second chunk failure'); END",
                (),
            )?;
        } else {
            fault.0.store(0, Ordering::SeqCst);
        }
        let error = file
            .pwrite_ranges(changes())
            .await
            .expect_err("injected write must fail");
        if sql_failure {
            assert!(
                matches!(error, Error::Database(tokio_rusqlite::rusqlite::Error::SqliteFailure(_, Some(ref message)))
                    if message == "injected second chunk failure"),
                "SQL cause was changed: {error:?}"
            );
            conn.execute("DROP TRIGGER injected_second_chunk", ())?;
        } else {
            assert!(
                matches!(error, Error::Io(ref e) if e.raw_os_error() == Some(1117)),
                "I/O cause was changed: {error:?}"
            );
        }
        // The unexpected device error stops the executor. Recovery is explicit;
        // verify persisted rollback through a fresh instance before writing again.
        let (sdk, fs, file) = if sql_failure {
            (sdk, fs, file)
        } else {
            drop(file);
            drop(fs);
            drop(sdk);
            let sdk = Vfs::open(VfsOptions::with_path(db.to_string_lossy())).await?;
            let fs = view(&base, &sdk, Arc::new(ReadFailure(AtomicUsize::new(100)))).await?;
            let stats = fs.lookup(1, "data.bin").await?.unwrap();
            let file = fs.open(stats.ino, libc::O_RDWR).await?;
            (sdk, fs, file)
        };
        assert_eq!(file.pread(0, original.len() as u64).await?, original);
        let after = file.fstat().await?;
        assert_eq!(
            (
                after.size,
                after.mtime,
                after.mtime_nsec,
                after.ctime,
                after.ctime_nsec
            ),
            (
                before.size,
                before.mtime,
                before.mtime_nsec,
                before.ctime,
                before.ctime_nsec
            )
        );
        assert_eq!(
            summary(&sdk).await?,
            counts,
            "failed write leaked storage rows"
        );
        // A fresh transaction must work after removing the fault.
        file.pwrite_ranges(changes()).await?;
        file.fsync().await?;
        let mut expected = original.clone();
        for range in changes() {
            let offset = range.offset as usize;
            expected[offset..offset + range.data.len()].copy_from_slice(&range.data);
        }
        assert_eq!(file.pread(0, expected.len() as u64).await?, expected);
        assert_eq!(fs.lookup(1, "alias.bin").await?.unwrap().ino, stats.ino);
        for name in ["data.bin", "alias.bin"] {
            assert_eq!(std::fs::read(base.join(name))?, original);
        }
        drop(file);
        fs.finalize().await?;
        let report = check(&conn, &CheckOpts::new(db).check_base(true))?;
        assert!(report.ok, "post-error integrity: {report:?}");
        println!(
            "PASS {}: rollback, typed cause and retry",
            if sql_failure { "SQL abort" } else { "COW I/O" }
        );
    }
    Ok(())
}
