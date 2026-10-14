//! Supported effects must have evidence; unsupported effects refuse before mutation.
#[allow(dead_code)]
#[path = "../src/storage.rs"]
mod storage;
#[allow(dead_code)]
#[path = "../src/tracking.rs"]
mod tracking;

use std::{collections::BTreeMap, sync::Arc};
use tracking::{Tracking, TrackingFS};
use vfs_core::{error::Error, FileSystem, TimeChange, Vfs, VfsOptions};

#[tokio::test]
async fn unsupported_mutations_leave_state_and_evidence_untouched() -> anyhow::Result<()> {
    let dir = tempfile::tempdir()?;
    let sdk = Vfs::open(VfsOptions::ephemeral()).await?;
    let (stats, file) = FileSystem::create_file(&sdk.fs, 1, "file", 0o644, 0, 0).await?;
    file.pwrite(0, b"original").await?;
    file.fsync().await?;
    let before = sdk.fs.getattr(stats.ino).await?.unwrap();
    let root_before = sdk.fs.getattr(1).await?.unwrap();
    let directory = FileSystem::mkdir(&sdk.fs, 1, "directory", 0o755, 0, 0).await?;
    let symbolic = FileSystem::symlink(&sdk.fs, 1, "symbolic", "file", 0, 0).await?;
    let record = dir.path().join("before.json");
    let tracked = Arc::new(Tracking::new(
        Arc::new(sdk.fs.clone()),
        record.clone(),
        dir.path().join("cas"),
        BTreeMap::new(),
        sdk.fs.chunk_size() as u64,
    ));
    let fs = TrackingFS(tracked.clone());
    for name in ["file", "directory", "symbolic"] {
        fs.lookup(1, name).await?;
    }
    macro_rules! refused {
        ($effect:expr) => {{
            let error = $effect.await.unwrap_err();
            assert!(matches!(error, Error::Io(ref value) if value.kind() == std::io::ErrorKind::Unsupported), "{error:?}");
            assert!(tracked.before.lock().await.is_empty());
            assert!(!record.exists());
        }};
    }
    refused!(fs.chown(stats.ino, Some(99), None));
    refused!(fs.utimens(stats.ino, TimeChange::Now, TimeChange::Set(10, 0)));
    refused!(fs.set_hidden(1, true));
    refused!(fs.chmod(1, 0o555));
    refused!(fs.chmod(symbolic.ino, 0o444));
    refused!(fs.link(stats.ino, 1, "alias"));
    refused!(fs.rename(1, "directory", 1, "moved"));
    refused!(fs.mknod(1, "fifo", 0o010644, 0, 0, 0));
    #[cfg(windows)]
    refused!(fs.symlink(1, "new-link", "file", 0, 0));
    fs.utimens(stats.ino, TimeChange::Omit, TimeChange::Omit)
        .await?;
    assert_eq!(file.pread(0, 64).await?, b"original");
    let after = sdk.fs.getattr(stats.ino).await?.unwrap();
    assert_eq!(
        (
            after.mode,
            after.uid,
            after.gid,
            after.mtime,
            after.ctime,
            after.nlink,
            after.hidden
        ),
        (
            before.mode,
            before.uid,
            before.gid,
            before.mtime,
            before.ctime,
            before.nlink,
            before.hidden
        )
    );
    let root_after = sdk.fs.getattr(1).await?.unwrap();
    assert_eq!(
        (root_after.mode, root_after.hidden),
        (root_before.mode, root_before.hidden)
    );
    assert_eq!(
        sdk.fs.getattr(symbolic.ino).await?.unwrap().mode,
        symbolic.mode
    );
    assert_eq!(
        sdk.fs.lookup(1, "directory").await?.unwrap().ino,
        directory.ino
    );
    for absent in ["alias", "moved", "fifo", "new-link"] {
        assert!(sdk.fs.lookup(1, absent).await?.is_none());
    }
    Ok(())
}
