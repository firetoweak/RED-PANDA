//! Process termination during COW seeding and repeated write/Flush transactions.
#![cfg(windows)]
use std::{
    fs::File,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};
use vfs_core::{
    error::Result,
    fs::BaseValidator,
    schema::integrity::{check, CheckOpts},
    FileSystem, HostFS, OverlayFS, PartialOriginMode, PartialOriginPolicy, Stats, Vfs, VfsOptions,
    WriteRange,
};

const SIZE: usize = 131072;
const OFFSETS: [usize; 2] = [7, 65543];

struct SeedBarrier {
    calls: AtomicUsize,
    root: PathBuf,
}
impl BaseValidator for SeedBarrier {
    fn validate(&self, _: &dyn FileSystem, _: &Stats) -> Result<()> {
        // Arm only after opening and flushing the origin. The second validation
        // is inside pwrite_ranges, after the first chunk was prepared in memory.
        let call = self.calls.fetch_add(1, Ordering::SeqCst);
        if call == 1 {
            std::fs::write(self.root.join("ready"), b"second seed inside transaction")?;
            loop {
                std::thread::sleep(Duration::from_millis(20));
            }
        }
        Ok(())
    }
}

async fn view(root: &Path, barrier: Option<Arc<SeedBarrier>>) -> Result<OverlayFS> {
    let base = root.join("base");
    let sdk = Vfs::open(VfsOptions::with_path(
        root.join("delta.db").to_string_lossy(),
    ))
    .await?;
    let mut fs = OverlayFS::new_with_partial_origin_policy(
        Arc::new(HostFS::new(&base)?),
        sdk.fs,
        PartialOriginPolicy::new(PartialOriginMode::On),
    );
    if let Some(barrier) = barrier {
        fs = fs.with_base_validator(barrier);
    }
    fs.init(base.to_str().unwrap()).await?;
    Ok(fs)
}

fn ranges(generation: u64) -> Vec<WriteRange> {
    let data: Vec<u8> = generation
        .to_le_bytes()
        .into_iter()
        .chain((!generation).to_le_bytes())
        .collect();
    OFFSETS
        .into_iter()
        .map(|offset| WriteRange {
            offset: offset as u64,
            data: data.clone(),
        })
        .collect()
}

struct Worker(Child);
impl Worker {
    fn start(root: &Path, phase: &str) -> std::io::Result<Self> {
        let log = File::create(root.join("worker.log"))?;
        Ok(Self(
            Command::new(std::env::current_exe()?)
                .args(["--exact", "write_worker", "--ignored", "--nocapture"])
                .env("VFS_WRITE_CRASH_ROOT", root)
                .env("VFS_WRITE_CRASH_PHASE", phase)
                .stdin(Stdio::null())
                .stdout(Stdio::from(log.try_clone()?))
                .stderr(Stdio::from(log))
                .spawn()?,
        ))
    }
    fn ready(&mut self, root: &Path) -> std::io::Result<()> {
        let start = Instant::now();
        while !root.join("ready").exists() {
            assert!(
                self.0.try_wait()?.is_none(),
                "worker exited: {}",
                std::fs::read_to_string(root.join("worker.log"))?
            );
            assert!(
                start.elapsed() < Duration::from_secs(15),
                "worker checkpoint timed out"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
        Ok(())
    }
    fn terminate(&mut self) -> std::io::Result<()> {
        self.0.kill()?;
        let start = Instant::now();
        loop {
            if let Some(status) = self.0.try_wait()? {
                assert!(!status.success(), "worker exited normally");
                return Ok(());
            }
            assert!(
                start.elapsed() < Duration::from_secs(15),
                "worker exit timed out"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        if matches!(self.0.try_wait(), Ok(None)) {
            let _ = self.0.kill();
        }
    }
}

async fn verify(root: &Path, seed: bool) -> Result<()> {
    let fs = view(root, None).await?;
    let stats = fs.lookup(1, "data.bin").await?.unwrap();
    let file = fs.open(stats.ino, libc::O_RDONLY).await?;
    let bytes = file.pread(0, SIZE as u64).await?;
    assert_eq!(bytes.len(), SIZE);
    let mut expected = vec![b'A'; SIZE];
    if !seed {
        let ack = std::fs::read(root.join("ack"))?;
        let acknowledged = u64::from_le_bytes(ack.try_into().expect("whole acknowledgment"));
        let generation = u64::from_le_bytes(bytes[OFFSETS[0]..OFFSETS[0] + 8].try_into().unwrap());
        assert!(
            generation >= acknowledged && generation <= acknowledged + 1,
            "recovered {generation}, last acknowledged Flush {acknowledged}"
        );
        for range in ranges(generation) {
            let start = range.offset as usize;
            expected[start..start + range.data.len()].copy_from_slice(&range.data);
        }
        println!("recovered generation {generation}, acknowledged {acknowledged}");
    }
    assert_eq!(bytes, expected, "partial transaction became visible");
    let alias = fs.lookup(1, "alias.bin").await?.unwrap();
    assert_eq!(alias.ino, stats.ino);
    for name in ["data.bin", "alias.bin"] {
        assert_eq!(
            std::fs::read(root.join("base").join(name))?,
            vec![b'A'; SIZE]
        );
    }
    assert_eq!(std::fs::read_dir(root.join("base"))?.count(), 2);
    let db = root.join("delta.db");
    let sdk = Vfs::open(VfsOptions::with_path(db.to_string_lossy())).await?;
    let conn = sdk.get_connection().await?;
    let report = check(&conn, &CheckOpts::new(db).check_base(true)).await?;
    assert!(report.ok, "post-crash integrity: {report:?}");
    if seed {
        for table in ["fs_data", "fs_chunk_override"] {
            let mut rows = conn
                .query(format!("SELECT COUNT(*) FROM {table}"), ())
                .await?;
            assert_eq!(
                rows.next().await?.unwrap().get::<i64>(0)?,
                0,
                "uncommitted chunk survived in {table}"
            );
        }
    }
    drop(file);
    fs.finalize().await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "kills only private workers; retains bounded fixtures"]
async fn interrupted_writes_recover_whole_transactions_and_flushed_versions() -> Result<()> {
    // The seed barrier is deterministic. Timed runs sample active write/commit/
    // Flush windows; they do not prove which individual instruction was killed.
    for (phase, delay) in [
        ("seed", 0),
        ("loop", 0),
        ("loop", 17),
        ("loop", 53),
        ("loop", 127),
        ("loop", 251),
    ] {
        let root = tempfile::tempdir()?.keep();
        std::fs::create_dir(root.join("base"))?;
        std::fs::write(root.join("base/data.bin"), vec![b'A'; SIZE])?;
        std::fs::hard_link(root.join("base/data.bin"), root.join("base/alias.bin"))?;
        println!("checkpoint {phase}/{delay}ms: {}", root.display());
        let mut worker = Worker::start(&root, phase)?;
        worker.ready(&root)?;
        if phase == "loop" {
            std::fs::write(root.join("go"), b"continue writes")?;
            std::thread::sleep(Duration::from_millis(delay));
        }
        worker.terminate()?;
        verify(&root, phase == "seed").await?;
        println!("PASS {phase}/{delay}ms");
    }
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "internal worker; invoked only by the crash test"]
async fn write_worker() -> Result<()> {
    let root = PathBuf::from(std::env::var_os("VFS_WRITE_CRASH_ROOT").expect("worker root"));
    let seed = std::env::var("VFS_WRITE_CRASH_PHASE").unwrap() == "seed";
    let barrier = Arc::new(SeedBarrier {
        calls: AtomicUsize::new(100),
        root: root.clone(),
    });
    let fs = view(&root, seed.then(|| barrier.clone())).await?;
    let stats = fs.lookup(1, "data.bin").await?.unwrap();
    let file = fs.open(stats.ino, libc::O_RDWR).await?;
    file.fsync().await?;
    if seed {
        barrier.calls.store(0, Ordering::SeqCst);
        file.pwrite_ranges(ranges(1)).await?;
        panic!("second-seed barrier did not execute");
    }
    for generation in 1u64.. {
        file.pwrite_ranges(ranges(generation)).await?;
        file.fsync().await?;
        // Publish acknowledgment only after fsync returned; atomic replacement
        // avoids observing a torn marker when the writer is terminated.
        std::fs::write(root.join("ack.next"), generation.to_le_bytes())?;
        std::fs::rename(root.join("ack.next"), root.join("ack"))?;
        if generation == 3 {
            std::fs::write(root.join("ready"), b"three generations flushed")?;
            let start = Instant::now();
            while !root.join("go").exists() {
                assert!(
                    start.elapsed() < Duration::from_secs(15),
                    "parent never released worker"
                );
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        }
    }
    unreachable!()
}
