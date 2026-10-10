//! Native I/O rejects failed barriers and retains the first unexpected core error.
#![cfg(all(windows, feature = "winfsp"))]
use anyhow::{ensure, Result};
use async_trait::async_trait;
use std::{
    fs::OpenOptions,
    io::{Read, Seek, SeekFrom, Write},
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};
use tokio_rusqlite::rusqlite::Connection;
use vfs_core::{
    error::{Error, Result as CoreResult},
    schema::integrity::{check, CheckOpts},
    BoxedFile, DirEntry, File, FileSystem, FilesystemStats, Stats, TimeChange, Vfs, VfsOptions,
};
use vfs_mount::{mount_fs, Backend, MountOpts};

#[derive(Clone, Copy, Debug, PartialEq)]
enum Operation {
    Read,
    Write,
    Flush,
    Cleanup,
}
struct Fault {
    operation: Operation,
    code: i32,
    armed: AtomicBool,
    attempts: AtomicUsize,
}
impl Fault {
    fn call(&self, operation: Operation) -> CoreResult<()> {
        self.attempts.fetch_add(1, Ordering::SeqCst);
        let selected = operation == self.operation
            || (operation == Operation::Flush && self.operation == Operation::Cleanup);
        if selected && self.armed.swap(false, Ordering::SeqCst) {
            return Err(std::io::Error::from_raw_os_error(self.code).into());
        }
        Ok(())
    }
}
struct FaultFile {
    inner: BoxedFile,
    fault: Arc<Fault>,
}
#[async_trait]
impl File for FaultFile {
    async fn pread(&self, offset: u64, size: u64) -> CoreResult<Vec<u8>> {
        self.fault.call(Operation::Read)?;
        self.inner.pread(offset, size).await
    }
    async fn pwrite(&self, offset: u64, data: &[u8]) -> CoreResult<()> {
        self.fault.call(Operation::Write)?;
        self.inner.pwrite(offset, data).await
    }
    async fn truncate(&self, size: u64) -> CoreResult<()> {
        self.inner.truncate(size).await
    }
    async fn fsync(&self) -> CoreResult<()> {
        self.fault.call(Operation::Flush)?;
        self.inner.fsync().await
    }
    async fn fstat(&self) -> CoreResult<Stats> {
        self.inner.fstat().await
    }
}
struct FaultFS {
    inner: Arc<dyn FileSystem>,
    fault: Arc<Fault>,
}
impl FaultFS {
    fn file(&self, inner: BoxedFile) -> BoxedFile {
        Arc::new(FaultFile {
            inner,
            fault: self.fault.clone(),
        })
    }
}
// Required filesystem methods retain the real core semantics. Only the three
// file I/O methods above inject a failure; no production fault-injection API.
#[async_trait]
impl FileSystem for FaultFS {
    async fn lookup(&self, parent: i64, name: &str) -> CoreResult<Option<Stats>> {
        self.inner.lookup(parent, name).await
    }
    async fn getattr(&self, ino: i64) -> CoreResult<Option<Stats>> {
        self.inner.getattr(ino).await
    }
    async fn readlink(&self, ino: i64) -> CoreResult<Option<String>> {
        self.inner.readlink(ino).await
    }
    async fn readdir(&self, ino: i64) -> CoreResult<Option<Vec<String>>> {
        self.inner.readdir(ino).await
    }
    async fn readdir_plus(&self, ino: i64) -> CoreResult<Option<Vec<DirEntry>>> {
        self.inner.readdir_plus(ino).await
    }
    async fn chmod(&self, ino: i64, mode: u32) -> CoreResult<()> {
        self.inner.chmod(ino, mode).await
    }
    async fn chown(&self, ino: i64, uid: Option<u32>, gid: Option<u32>) -> CoreResult<()> {
        self.inner.chown(ino, uid, gid).await
    }
    async fn utimens(&self, ino: i64, atime: TimeChange, mtime: TimeChange) -> CoreResult<()> {
        self.inner.utimens(ino, atime, mtime).await
    }
    async fn mkdir(
        &self,
        parent: i64,
        name: &str,
        mode: u32,
        uid: u32,
        gid: u32,
    ) -> CoreResult<Stats> {
        self.inner.mkdir(parent, name, mode, uid, gid).await
    }
    async fn mknod(
        &self,
        parent: i64,
        name: &str,
        mode: u32,
        rdev: u64,
        uid: u32,
        gid: u32,
    ) -> CoreResult<Stats> {
        self.inner.mknod(parent, name, mode, rdev, uid, gid).await
    }
    async fn symlink(
        &self,
        parent: i64,
        name: &str,
        target: &str,
        uid: u32,
        gid: u32,
    ) -> CoreResult<Stats> {
        self.inner.symlink(parent, name, target, uid, gid).await
    }
    async fn unlink(&self, parent: i64, name: &str) -> CoreResult<()> {
        self.inner.unlink(parent, name).await
    }
    async fn rmdir(&self, parent: i64, name: &str) -> CoreResult<()> {
        self.inner.rmdir(parent, name).await
    }
    async fn link(&self, ino: i64, parent: i64, name: &str) -> CoreResult<Stats> {
        self.inner.link(ino, parent, name).await
    }
    async fn rename(
        &self,
        parent: i64,
        name: &str,
        newparent: i64,
        newname: &str,
    ) -> CoreResult<()> {
        self.inner.rename(parent, name, newparent, newname).await
    }
    async fn statfs(&self) -> CoreResult<FilesystemStats> {
        self.inner.statfs().await
    }
    async fn drain_all(&self) -> CoreResult<()> {
        self.inner.drain_all().await
    }
    async fn finalize(&self) -> CoreResult<()> {
        self.inner.finalize().await
    }
    async fn open(&self, ino: i64, flags: i32) -> CoreResult<BoxedFile> {
        Ok(self.file(self.inner.open(ino, flags).await?))
    }
    async fn create_file(
        &self,
        parent: i64,
        name: &str,
        mode: u32,
        uid: u32,
        gid: u32,
    ) -> CoreResult<(Stats, BoxedFile)> {
        let (stats, file) = self.inner.create_file(parent, name, mode, uid, gid).await?;
        Ok((stats, self.file(file)))
    }
    async fn forget(&self, ino: i64, count: u64) {
        self.inner.forget(ino, count).await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "requires WinFsp; injects errors without filling the disk"]
async fn native_errors_preserve_failure_and_durability_contracts() -> Result<()> {
    for (operation, code) in [
        (Operation::Read, 1117),
        (Operation::Write, 1117),
        (Operation::Flush, 1117),
        (Operation::Write, 112),
        (Operation::Flush, 112),
        (Operation::Cleanup, 1117),
        (Operation::Cleanup, 112),
    ] {
        let root = tempfile::tempdir()?.keep();
        println!("case {operation:?}/{code}: {}", root.display());
        let db = root.join("delta.db");
        let sdk = Vfs::open(VfsOptions::with_path(db.to_string_lossy())).await?;
        let (stats, file) = FileSystem::create_file(&sdk.fs, 1, "data.bin", 0o644, 0, 0).await?;
        file.pwrite(0, b"Original").await?;
        file.fsync().await?;
        drop(file);
        let fault = Arc::new(Fault {
            operation,
            code,
            armed: AtomicBool::new(false),
            attempts: AtomicUsize::new(0),
        });
        let fs = Arc::new(FaultFS {
            inner: Arc::new(sdk.fs.clone()),
            fault: fault.clone(),
        });
        let point = root.join("mounted");
        let handle = mount_fs(fs, MountOpts::new(point.clone(), Backend::WinFsp)).await?;
        let trigger = fault.clone();
        let operations = tokio::task::spawn_blocking(move || -> Result<()> {
            // All native handles are dropped before explicit unmount, including
            // assertion/error paths. This process is never forcibly terminated.
            let mut native = OpenOptions::new()
                .read(true)
                .write(true)
                .open(point.join("data.bin"))?;
            if operation == Operation::Flush {
                native.write_all(b"Changed!")?;
            }
            trigger.armed.store(true, Ordering::SeqCst);
            if operation == Operation::Cleanup {
                // Cleanup has no error return to CloseHandle; unmount must surface it.
                drop(native);
                return Ok(());
            }
            let failed = match operation {
                Operation::Read => native.read_exact(&mut [0; 8]),
                Operation::Write => native.write_all(b"Changed!"),
                Operation::Flush => native.sync_all(),
                Operation::Cleanup => unreachable!(),
            };
            let error = failed.expect_err("injected operation reported success");
            if code == 112 {
                ensure!(
                    error.raw_os_error() == Some(112),
                    "disk-full cause changed: {error}"
                );
                if operation == Operation::Write {
                    native.write_all(b"Changed!")?;
                }
                native.sync_all()?;
                native.seek(SeekFrom::Start(0))?;
                let mut bytes = [0; 8];
                native.read_exact(&mut bytes)?;
                ensure!(&bytes == b"Changed!", "successful retry lost data");
            } else {
                ensure!(
                    error.raw_os_error() == Some(1359),
                    "unexpected fatal native code: {error}"
                );
                let attempts = trigger.attempts.load(Ordering::SeqCst);
                native.seek(SeekFrom::Start(0))?;
                let next = native
                    .read_exact(&mut [0; 8])
                    .expect_err("fatal mount continued serving");
                ensure!(
                    next.raw_os_error() == Some(1359),
                    "fatal latch changed native status: {next}"
                );
                ensure!(
                    trigger.attempts.load(Ordering::SeqCst) == attempts,
                    "fatal mount reached core I/O again"
                );
            }
            Ok(())
        })
        .await;
        let start = Instant::now();
        while operation == Operation::Cleanup
            && fault.armed.load(Ordering::SeqCst)
            && start.elapsed() < Duration::from_secs(2)
        {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        let cleanup_observed = !fault.armed.load(Ordering::SeqCst);
        let teardown = handle.unmount().await;
        operations??;
        ensure!(
            operation != Operation::Cleanup || cleanup_observed,
            "Cleanup fault did not execute before explicit unmount"
        );
        if code == 112 && operation != Operation::Cleanup {
            teardown?;
        } else {
            let error = teardown.expect_err("fatal mount teardown reported success");
            ensure!(
                matches!(error.downcast_ref::<Error>(), Some(Error::Io(e)) if e.raw_os_error() == Some(code)),
                "original fatal cause was changed: {error:#}"
            );
        }
        let expected =
            if operation == Operation::Flush || (code == 112 && operation != Operation::Cleanup) {
                b"Changed!"
            } else {
                b"Original"
            };
        ensure!(
            FileSystem::open(&sdk.fs, stats.ino, libc::O_RDONLY)
                .await?
                .pread(0, 8)
                .await?
                == expected,
            "unexpected data after I/O failure"
        );
        let conn = Connection::open(&db)?;
        let report = check(&conn, &CheckOpts::new(db))?;
        ensure!(report.ok, "post-I/O-error integrity: {report:?}");
        println!("PASS {operation:?}/{code}: native failure, teardown and integrity");
    }
    Ok(())
}
