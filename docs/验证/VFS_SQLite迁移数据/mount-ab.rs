use anyhow::Result;
use std::{io::Write, path::Path, sync::Arc, time::Instant};
use vfs_core::{FileSystem, HostFS, OverlayFS, PartialOriginMode, PartialOriginPolicy, Vfs, VfsOptions};
use vfs_mount::{mount_fs, Backend, MountOpts};

fn summarize(name: &str, mut samples: Vec<f64>, bytes: usize) -> serde_json::Value {
    let elapsed = samples.iter().sum::<f64>();
    samples.sort_by(f64::total_cmp);
    serde_json::json!({"workload":name,"operations":samples.len(),"elapsed_ms":elapsed/1000.0,"ops_per_second":samples.len() as f64*1e6/elapsed,"p50_us":samples[samples.len()/2],"p95_us":samples[(samples.len()*95/100).min(samples.len()-1)],"bytes_per_operation":bytes})
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> Result<()> {
    let trial = std::env::args().nth(1).unwrap_or_else(|| "0".into());
    if trial == "fixture" { return fixture(Path::new(&std::env::args().nth(2).unwrap())).await; }
    if trial == "verify" { return verify_fixture(Path::new(&std::env::args().nth(2).unwrap())).await; }
    let temporary = tempfile::tempdir()?;
    let base = temporary.path().join("base");
    std::fs::create_dir(&base)?;
    let original = vec![b'A'; 131072];
    std::fs::write(base.join("host.bin"), &original)?;
    let sdk = Vfs::open(VfsOptions::with_path(temporary.path().join("delta.db").to_str().unwrap())).await?;
    for size in [4096, 65536] {
        for index in 0..64 {
            let (_, file) = FileSystem::create_file(&sdk.fs, 1, &format!("file-{size}-{index}"), 0o644, 0, 0).await?;
            file.pwrite(0, &vec![7; size]).await?;
        }
    }
    sdk.fs.drain_all().await?;
    let overlay = Arc::new(OverlayFS::new_with_partial_origin_policy(Arc::new(HostFS::new(&base)?), sdk.fs, PartialOriginPolicy::new(PartialOriginMode::On)));
    overlay.init(base.to_str().unwrap()).await?;
    let point = temporary.path().join("mount");
    let mount = mount_fs(overlay, MountOpts::new(point.clone(), Backend::WinFsp)).await?;
    let operations = tokio::task::spawn_blocking(move || -> Result<Vec<serde_json::Value>> {
        let mut report = Vec::new();
        for size in [4096, 65536] {
            // Open/read/close exercises the real adapter; FlushAndPurgeOnCleanup
            // prevents the test from being only a long-lived Windows cache hit.
            for index in 0..128 { assert_eq!(std::fs::read(point.join(format!("file-{size}-{}", index%64)))?, vec![7; size]); }
            let count = if size == 4096 { 1500 } else { 750 };
            let mut samples = Vec::with_capacity(count);
            for index in 0..count {
                let start = Instant::now();
                let bytes = std::fs::read(point.join(format!("file-{size}-{}", index%64)))?;
                samples.push(start.elapsed().as_secs_f64()*1e6);
                assert_eq!(bytes, vec![7; size]);
            }
            report.push(summarize(&format!("open-read-close-{size}"), samples, size));
        }
        let mut samples = Vec::with_capacity(3000);
        for index in 0..3000 {
            let start = Instant::now();
            let stats = std::fs::metadata(point.join(format!("file-4096-{}", index%64)))?;
            samples.push(start.elapsed().as_secs_f64()*1e6);
            assert_eq!(stats.len(), 4096);
        }
        report.push(summarize("metadata", samples, 0));
        let mut samples = Vec::with_capacity(300);
        let bytes = vec![9; 4096];
        for index in 0..300 {
            let start = Instant::now();
            let from = point.join(format!("new-{index}"));
            let to = point.join(format!("moved-{index}"));
            let mut file = std::fs::File::create(&from)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            drop(file);
            std::fs::rename(&from, &to)?;
            std::fs::remove_file(&to)?;
            samples.push(start.elapsed().as_secs_f64()*1e6);
        }
        report.push(summarize("create-write-sync-rename-delete", samples, 4096));
        let mut samples = Vec::with_capacity(300);
        for _ in 0..300 {
            let start = Instant::now();
            let mut file = std::fs::OpenOptions::new().write(true).open(point.join("host.bin"))?;
            file.write_all(b"partial")?;
            file.sync_all()?;
            drop(file);
            samples.push(start.elapsed().as_secs_f64()*1e6);
        }
        report.push(summarize("partial-write-sync-close", samples, 7));
        let gate = Arc::new(std::sync::Barrier::new(4));
        for client in 0..4 { std::fs::write(point.join(format!("mixed-{client}")), vec![8;4096])?; }
        let start = Instant::now();
        let clients = (0..4).map(|client| {
            let point = point.clone();
            let gate = gate.clone();
            std::thread::spawn(move || -> Result<(Vec<f64>, Vec<f64>)> {
                let mut reads = Vec::new();
                let mut writes = Vec::new();
                gate.wait();
                for index in 0..600 {
                    let start = Instant::now();
                    if index % 5 == 0 {
                        let mut file = std::fs::OpenOptions::new().write(true).open(point.join(format!("mixed-{client}")))?;
                        file.write_all(&vec![8;4096])?;
                        file.sync_all()?;
                        drop(file);
                        writes.push(start.elapsed().as_secs_f64()*1e6);
                    } else {
                        let bytes = std::fs::read(point.join(format!("file-4096-{}", index%64)))?;
                        reads.push(start.elapsed().as_secs_f64()*1e6);
                        assert_eq!(bytes, vec![7;4096]);
                    }
                }
                Ok((reads, writes))
            })
        }).collect::<Vec<_>>();
        let mut reads = Vec::new();
        let mut writes = Vec::new();
        for client in clients {
            let (read, write) = client.join().unwrap()?;
            reads.extend(read); writes.extend(write);
        }
        let wall_ms = start.elapsed().as_secs_f64()*1000.0;
        for mut row in [summarize("mixed4-read", reads, 4096), summarize("mixed4-write-sync", writes, 4096)] {
            row["elapsed_ms"] = wall_ms.into();
            row["ops_per_second"] = (row["operations"].as_u64().unwrap() as f64*1000.0/wall_ms).into();
            row["concurrent_clients"] = 4.into();
            report.push(row);
        }
        Ok(report)
    }).await;
    let teardown = mount.unmount().await;
    teardown?;
    let workloads = operations??;
    assert_eq!(std::fs::read(base.join("host.bin"))?, original);
    println!("{}", serde_json::json!({"engine":env!("CARGO_PKG_NAME"), "trial":trial,"workloads":workloads}));
    Ok(())
}

async fn fixture(directory: &Path) -> Result<()> {
    std::fs::create_dir_all(directory.join("base"))?;
    let base = directory.join("base");
    std::fs::write(base.join("host.bin"), vec![b'H'; 131072])?;
    std::fs::write(base.join("removed.txt"), b"still on host")?;
    let sdk = Vfs::open(VfsOptions::with_path(directory.join("source.db").to_str().unwrap())).await?;
    let (stats, content) = FileSystem::create_file(&sdk.fs, 1, "content", 0o644, 17, 18).await?;
    content.pwrite(0, &vec![b'I'; 4096]).await?;
    FileSystem::link(&sdk.fs, stats.ino, 1, "alias").await?;
    FileSystem::symlink(&sdk.fs, 1, "symbolic", "/content", 17, 18).await?;
    let root = sdk.capture_root("inline-before-overlay").await?;
    content.pwrite(0, &vec![b'C'; 131072]).await?;
    let view = OverlayFS::new_with_partial_origin_policy(Arc::new(HostFS::new(&base)?), sdk.fs.clone(), PartialOriginPolicy::new(PartialOriginMode::On));
    view.init(base.to_str().unwrap()).await?;
    let host = view.lookup(1, "host.bin").await?.unwrap();
    let partial = view.open(host.ino, libc::O_RDWR).await?;
    partial.pwrite(65534, b"crossing").await?;
    partial.fsync().await?;
    view.unlink(1, "removed.txt").await?;
    sdk.snapshot_into(&directory.join("artifact.db")).await?;
    std::fs::write(directory.join("fixture.json"), serde_json::to_vec(&serde_json::json!({"root_seq":root.through_seq}))?)?;
    view.finalize().await?;
    println!("fixture created with {}", env!("CARGO_PKG_NAME"));
    Ok(())
}

async fn verify_fixture(directory: &Path) -> Result<()> {
    let artifact = directory.join("artifact.db");
    let sdk = Vfs::open_read_only(&artifact).await?;
    assert_eq!(sdk.fs.open("/content").await?.pread(0, 131072).await?, vec![b'C'; 131072]);
    assert_eq!(sdk.fs.open("/alias").await?.pread(0, 131072).await?, vec![b'C'; 131072]);
    assert_eq!(sdk.fs.readlink("/symbolic").await?.unwrap(), "/content");
    let mut expected = vec![b'H'; 131072];
    expected[65534..65542].copy_from_slice(b"crossing");
    let base = directory.join("base");
    let view = OverlayFS::new_with_partial_origin_policy(Arc::new(HostFS::new(&base)?), sdk.fs.clone(), PartialOriginPolicy::new(PartialOriginMode::On));
    view.load().await?;
    assert!(view.lookup(1, "removed.txt").await?.is_none());
    let stats = view.lookup(1, "host.bin").await?.unwrap();
    assert_eq!(view.open(stats.ino, libc::O_RDONLY).await?.pread(0, 131072).await?, expected);
    let manifest: serde_json::Value = serde_json::from_slice(&std::fs::read(directory.join("fixture.json"))?)?;
    let seq = manifest["root_seq"].as_i64().unwrap();
    sdk.validate_target(seq).await?;
    let staging = directory.join("reconstructed.db");
    std::fs::copy(&artifact, &staging)?;
    Vfs::reconstruct_to(&staging, seq).await?;
    let replay = Vfs::open_read_only(staging).await?;
    assert_eq!(replay.fs.open("/content").await?.pread(0, 131072).await?, vec![b'I'; 4096]);
    assert_eq!(std::fs::read(base.join("host.bin"))?, vec![b'H'; 131072]);
    assert_eq!(std::fs::read(base.join("removed.txt"))?, b"still on host");
    println!("fixture verified with {}", env!("CARGO_PKG_NAME"));
    Ok(())
}
