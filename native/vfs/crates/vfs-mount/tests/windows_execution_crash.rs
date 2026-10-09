//! Process-crash contracts. Workers are killed without running Rust Drop/unmount.
#![cfg(all(windows, feature = "winfsp"))]
use anyhow::{bail, ensure, Context, Result};
use std::{
    fs::{File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    os::windows::{fs::OpenOptionsExt, io::AsRawHandle},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Arc,
    time::{Duration, Instant},
};
use vfs_core::{
    schema::integrity::{check, CheckOpts},
    FileSystem, HostFS, OverlayFS, PartialOriginMode, PartialOriginPolicy, Vfs, VfsOptions,
};
use vfs_mount::{mount_fs, Backend, MountHandle, MountOpts};

const SIZE: usize = 131072;
const OFFSET: usize = 7;

#[link(name = "kernel32")]
unsafe extern "system" {
    fn SetFileInformationByHandle(
        file: *mut std::ffi::c_void,
        class: i32,
        info: *const std::ffi::c_void,
        size: u32,
    ) -> i32;
}
fn disposition(file: &File, deleting: bool) -> Result<()> {
    // FILE_DISPOSITION_INFO contains one BOOLEAN, not a Win32 BOOL.
    let flag = u8::from(deleting);
    // SAFETY: the owned file handle remains open; class 4 reads this one-byte structure.
    let success = unsafe {
        SetFileInformationByHandle(file.as_raw_handle(), 4, (&flag as *const u8).cast(), 1)
    };
    if success == 0 {
        return Err(std::io::Error::last_os_error()).context("set delete disposition");
    }
    Ok(())
}

fn pending_open_is_denied(point: &Path) -> Result<()> {
    let error = std::fs::read(point.join("ephemeral.txt"))
        .expect_err("delete-pending file opened through a new handle");
    ensure!(
        matches!(error.raw_os_error(), Some(5 | 303)),
        "unexpected error opening pending-delete file: {error}"
    );
    Ok(())
}

async fn view(root: &Path) -> Result<Arc<OverlayFS>> {
    let base = root.join("base");
    let sdk = Vfs::open(VfsOptions::with_path(
        root.join("delta.db").to_string_lossy(),
    ))
    .await?;
    let fs = Arc::new(OverlayFS::new_with_partial_origin_policy(
        Arc::new(HostFS::new(&base)?),
        sdk.fs,
        PartialOriginPolicy::new(PartialOriginMode::On),
    ));
    fs.init(base.to_str().unwrap()).await?;
    Ok(fs)
}

async fn mount(root: &Path, fs: Arc<OverlayFS>) -> Result<MountHandle> {
    mount_fs(fs, MountOpts::new(root.join("mounted"), Backend::WinFsp)).await
}

fn write_open(point: &Path, flush: bool) -> Result<File> {
    let mut file = OpenOptions::new()
        .read(true)
        .write(true)
        .open(point.join("Original.bin"))?;
    file.seek(SeekFrom::Start(OFFSET as u64))?;
    file.write_all(b"XYZ")?;
    if flush {
        file.sync_all()?;
    }
    Ok(file)
}

// Own only the process we spawned, including failure paths in the test harness.
struct Worker {
    child: Child,
    log: PathBuf,
}
impl Worker {
    fn start(root: &Path, phase: &str, test: &str) -> Result<Self> {
        let log_path = root.join(format!("{test}.log"));
        let log = File::create(&log_path)?;
        Ok(Self {
            child: Command::new(std::env::current_exe()?)
                .args(["--exact", test, "--ignored", "--nocapture"])
                .env("VFS_CRASH_ROOT", root)
                .env("VFS_CRASH_PHASE", phase)
                .stdin(Stdio::null())
                .stdout(Stdio::from(log.try_clone()?))
                .stderr(Stdio::from(log))
                .spawn()?,
            log: log_path,
        })
    }
    fn wait_ready(&mut self, root: &Path, marker: &str) -> Result<()> {
        let start = Instant::now();
        while !root.join(marker).exists() {
            if let Some(status) = self.child.try_wait()? {
                bail!(
                    "worker exited before checkpoint ({status}): {}",
                    std::fs::read_to_string(&self.log)?
                );
            }
            ensure!(
                start.elapsed() < Duration::from_secs(15),
                "worker checkpoint timed out"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
        Ok(())
    }
    fn terminate(&mut self) -> Result<()> {
        self.child.kill()?;
        ensure!(
            !self.wait_exit()?.success(),
            "worker exited normally instead of being killed"
        );
        Ok(())
    }
    fn wait_exit(&mut self) -> Result<std::process::ExitStatus> {
        let start = Instant::now();
        loop {
            if let Some(status) = self.child.try_wait()? {
                return Ok(status);
            }
            ensure!(
                start.elapsed() < Duration::from_secs(15),
                "worker {} exit timed out; evidence at {}",
                self.child.id(),
                self.log.display()
            );
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        if matches!(self.child.try_wait(), Ok(None)) {
            let _ = self.child.kill();
        }
    }
}

fn fixture() -> Result<PathBuf> {
    let root = tempfile::tempdir()?;
    std::fs::create_dir(root.path().join("base"))?;
    std::fs::write(root.path().join("base/Original.bin"), vec![b'A'; SIZE])?;
    std::fs::hard_link(
        root.path().join("base/Original.bin"),
        root.path().join("base/alias.bin"),
    )?;
    std::fs::write(root.path().join("base/orphan.bin"), vec![b'A'; SIZE])?;
    // Retain bounded evidence, including a failed/stale mount; never recurse into it.
    Ok(root.keep())
}

fn host_unchanged(root: &Path) -> Result<()> {
    for name in ["Original.bin", "alias.bin", "orphan.bin"] {
        ensure!(
            std::fs::read(root.join("base").join(name))? == vec![b'A'; SIZE],
            "host bytes changed: {name}"
        );
    }
    ensure!(
        std::fs::read_dir(root.join("base"))?.count() == 3,
        "host namespace changed"
    );
    Ok(())
}

async fn inspect(root: &Path, fs: &OverlayFS, phase: &str) -> Result<()> {
    let original = fs
        .lookup(1, "ORIGINAL.BIN")
        .await?
        .context("original missing after crash")?;
    let bytes = fs
        .open(original.ino, libc::O_RDONLY)
        .await?
        .pread(0, SIZE as u64)
        .await?;
    let mut changed = vec![b'A'; SIZE];
    changed[OFFSET..OFFSET + 3].copy_from_slice(b"XYZ");
    if phase == "before-flush" {
        ensure!(
            bytes == changed || bytes == vec![b'A'; SIZE],
            "unflushed write recovered a torn file"
        );
    } else {
        ensure!(bytes == changed, "successful Flush was lost: {phase}");
    }
    let alias_name = if matches!(phase, "namespace" | "pending-delete") {
        "renamed.bin"
    } else {
        "alias.bin"
    };
    let alias = fs
        .lookup(1, alias_name)
        .await?
        .context("hard-link alias missing after crash")?;
    ensure!(alias.ino == original.ino, "hard-link identity diverged");
    if matches!(phase, "namespace" | "pending-delete") {
        ensure!(
            fs.lookup(1, "alias.bin").await?.is_none(),
            "rename whiteout lost"
        );
        let ephemeral = fs.lookup(1, "ephemeral.txt").await?;
        if phase == "namespace" {
            ensure!(ephemeral.is_none(), "completed deletion resurfaced");
        } else if let Some(stats) = ephemeral {
            let data = fs
                .open(stats.ino, libc::O_RDONLY)
                .await?
                .pread(0, 128)
                .await?;
            ensure!(
                data == b"temporary private bytes",
                "pending-delete recovery lost flushed content"
            );
            println!("OBSERVED pending DeleteFile intent lost with owner; complete flushed file recovered");
        }
    }
    if phase == "core-unlink-open" {
        ensure!(
            fs.lookup(1, "orphan.bin").await?.is_none(),
            "core-unlinked origin resurfaced"
        );
    }
    if phase.starts_with("disposition-") {
        let ephemeral = fs.lookup(1, "ephemeral.txt").await?;
        if matches!(
            phase,
            "disposition-close"
                | "disposition-retoggle"
                | "disposition-command"
                | "disposition-duplicate"
                | "disposition-second-handle"
        ) {
            ensure!(
                ephemeral.is_none(),
                "completed disposition deletion resurfaced"
            );
        } else {
            let stats = ephemeral.context("cancelled or uncompleted file missing")?;
            ensure!(
                fs.open(stats.ino, libc::O_RDONLY)
                    .await?
                    .pread(0, 128)
                    .await?
                    == b"temporary private bytes",
                "disposition recovery changed flushed bytes"
            );
        }
    }
    host_unchanged(root)?;
    let db = root.join("delta.db");
    let sdk = Vfs::open(VfsOptions::with_path(db.to_string_lossy())).await?;
    let conn = sdk.get_connection().await?;
    let report = check(&conn, &CheckOpts::new(db).check_base(true)).await?;
    ensure!(report.ok, "post-crash integrity failed: {report:?}");
    let mut rows = conn
        .query("SELECT COUNT(*) FROM fs_inode WHERE nlink = 0", ())
        .await?;
    let orphan_count: i64 = rows.next().await?.unwrap().get(0)?;
    ensure!(orphan_count == 0, "crash recovery retained orphan inodes");
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "requires installed WinFsp; kills only private test worker processes"]
async fn abrupt_execution_keeps_committed_view_and_host_unchanged() -> Result<()> {
    // Serialize mounts; each case uses its own bounded ordinary files and database.
    for phase in [
        "command",
        "before-flush",
        "after-flush",
        "namespace",
        "core-unlink-open",
        "pending-delete",
        "disposition-close",
        "disposition-cancel-close",
        "disposition-cancel-owner",
        "disposition-command",
        "disposition-duplicate",
        "disposition-owner",
        "disposition-retoggle",
        "disposition-second-handle",
    ] {
        let root_path = fixture()?;
        let root = root_path.as_path();
        println!("checkpoint {phase}: {}", root.display());
        let mut owner = Worker::start(root, phase, "mount_worker")?;
        owner.wait_ready(root, "mounted-ready")?;
        let mut command = Worker::start(root, phase, "command_worker")?;
        if let Err(error) = command.wait_ready(root, "ready") {
            std::fs::write(root.join("stop"), b"surface owner failure")?;
            let exit = owner.wait_exit();
            return Err(error.context(format!(
                "owner exit {exit:?}: {}",
                std::fs::read_to_string(&owner.log)?
            )));
        }
        if matches!(
            phase,
            "disposition-command" | "disposition-duplicate" | "disposition-second-handle"
        ) {
            command.terminate()?;
            // Native Cleanup can finish asynchronously after process exit.
            // The owner checks via core and publishes a barrier after unlink.
            std::fs::write(root.join("await-deletion"), b"commit completed Cleanup")?;
            owner.wait_ready(root, "deletion-durable")?;
            owner.terminate()?;
        } else if phase == "command" {
            command.terminate()?;
            let point = root.join("mounted");
            let bytes =
                tokio::task::spawn_blocking(move || std::fs::read(point.join("Original.bin")))
                    .await??;
            ensure!(
                &bytes[OFFSET..OFFSET + 3] == b"XYZ",
                "live owner lost flushed bytes after command death"
            );
            std::fs::write(root.join("stop"), b"normal owner shutdown")?;
            ensure!(
                owner.wait_exit()?.success(),
                "owner failed clean shutdown after command death"
            );
        } else {
            let owner_exit = owner.terminate();
            let command_exit = command.terminate();
            owner_exit?;
            command_exit?;
        }
        let fs = view(root).await?;
        inspect(root, &fs, phase)
            .await
            .with_context(|| format!("owner death at {phase}"))?;
        // Reuse the same mountpoint: the dead owner must not leave an unusable mount.
        let handle = mount(root, fs.clone()).await?;
        let point = root.join("mounted");
        let native =
            tokio::task::spawn_blocking(move || std::fs::read(point.join("Original.bin"))).await?;
        let teardown = handle.unmount().await;
        ensure!(
            native?.len() == SIZE,
            "recovered mount could not read the file"
        );
        teardown?;
        println!("PASS {phase}: recovered view, integrity and remount");
    }
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "internal worker; invoked only by the crash test"]
async fn mount_worker() -> Result<()> {
    let root = PathBuf::from(std::env::var_os("VFS_CRASH_ROOT").context("missing worker root")?);
    let fs = view(&root).await?;
    let handle = mount(&root, fs.clone()).await?;
    // Serving process never opens native handles into its own mount.
    std::fs::write(root.join("mounted-ready"), b"mounted")?;
    while !root.join("stop").exists() {
        if root.join("await-deletion").exists()
            && !root.join("deletion-durable").exists()
            && fs.lookup(1, "ephemeral.txt").await?.is_none()
        {
            let original = fs.lookup(1, "Original.bin").await?.unwrap();
            fs.open(original.ino, libc::O_RDWR).await?.fsync().await?;
            std::fs::write(root.join("deletion-durable"), b"Cleanup persisted")?;
        }
        if root.join("unlink-open").exists() && !root.join("unlink-done").exists() {
            // Core unlink is immediate even while the native client retains the file.
            fs.unlink(1, "orphan.bin").await?;
            let original = fs.lookup(1, "Original.bin").await?.unwrap();
            fs.open(original.ino, libc::O_RDWR).await?.fsync().await?;
            std::fs::write(root.join("unlink-done"), b"durable core unlink")?;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    handle.unmount().await
}

#[test]
#[ignore = "internal worker; invoked only by the crash test"]
fn command_worker() -> Result<()> {
    let root = PathBuf::from(std::env::var_os("VFS_CRASH_ROOT").context("missing worker root")?);
    let phase = std::env::var("VFS_CRASH_PHASE")?;
    let point = root.join("mounted");
    let mut files = vec![write_open(&point, phase != "before-flush")?];
    if phase.starts_with("disposition-") {
        let mut file = OpenOptions::new()
            .create_new(true)
            .read(true)
            .write(true)
            // SetFileInformationByHandle requires DELETE access.
            .access_mode(0xc0010000)
            .open(point.join("ephemeral.txt"))?;
        file.write_all(b"temporary private bytes")?;
        file.sync_all()?;
        let second = if phase == "disposition-second-handle" {
            Some(File::open(point.join("ephemeral.txt"))?)
        } else {
            None
        };
        disposition(&file, true)?;
        if matches!(
            phase.as_str(),
            "disposition-cancel-close" | "disposition-cancel-owner" | "disposition-retoggle"
        ) {
            disposition(&file, false)?;
            ensure!(
                std::fs::read(point.join("ephemeral.txt"))? == b"temporary private bytes",
                "cancelled disposition still blocked new opens"
            );
        }
        if phase == "disposition-retoggle" {
            disposition(&file, true)?;
        }
        if matches!(
            phase.as_str(),
            "disposition-close" | "disposition-cancel-close" | "disposition-retoggle"
        ) {
            drop(file);
            files[0].sync_all()?;
        } else if phase == "disposition-duplicate" {
            let mut duplicate = file.try_clone()?;
            drop(file);
            pending_open_is_denied(&point)?;
            duplicate.seek(SeekFrom::Start(0))?;
            let mut bytes = Vec::new();
            duplicate.read_to_end(&mut bytes)?;
            ensure!(
                bytes == b"temporary private bytes",
                "duplicate handle lost pending bytes"
            );
            files.push(duplicate);
        } else if let Some(mut second) = second {
            drop(file);
            pending_open_is_denied(&point)?;
            let mut bytes = Vec::new();
            second.read_to_end(&mut bytes)?;
            ensure!(
                bytes == b"temporary private bytes",
                "other open handle lost pending bytes"
            );
            files.push(second);
        } else {
            files.push(file);
        }
    }
    if matches!(phase.as_str(), "namespace" | "pending-delete") {
        std::fs::rename(point.join("alias.bin"), point.join("renamed.bin"))
            .context("rename alias")?;
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(point.join("ephemeral.txt"))?;
        file.write_all(b"temporary private bytes")
            .context("write ephemeral")?;
        file.sync_all().context("flush ephemeral")?;
        std::fs::remove_file(point.join("ephemeral.txt")).context("request ephemeral deletion")?;
        if phase == "pending-delete" {
            files.push(file);
        } else {
            drop(file);
            // Flush after completed Cleanup; delete-pending is a different boundary.
            files[0]
                .sync_all()
                .context("flush after completed ephemeral Cleanup")?;
        }
    }
    if phase == "core-unlink-open" {
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(point.join("orphan.bin"))?;
        file.write_all(b"Q")?;
        file.sync_all()?;
        std::fs::write(root.join("unlink-open"), b"request core unlink")?;
        let start = Instant::now();
        while !root.join("unlink-done").exists() {
            ensure!(
                start.elapsed() < Duration::from_secs(10),
                "core unlink checkpoint timed out"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
        files.push(file);
    }
    std::fs::write(root.join("ready"), b"command flushed")?;
    loop {
        std::hint::black_box(&files);
        std::thread::sleep(Duration::from_millis(100));
    }
}
