use super::*;
use crate::error::Error;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

async fn create_frozen_artifact(temp_dir: &Path) -> PathBuf {
    let source_path = temp_dir.join("source.db");
    {
        let source = fs::Vfs::new(source_path.to_str().unwrap()).await.unwrap();
        let (_, file) =
            FileSystem::create_file(&source, 1, "artifact.txt", DEFAULT_FILE_MODE, 1000, 1000)
                .await
                .unwrap();
        file.pwrite(0, b"frozen artifact").await.unwrap();
        source.finalize().await.unwrap();
    }

    let artifact_path = temp_dir.join("artifact.db");
    std::fs::copy(source_path, &artifact_path).unwrap();
    artifact_path
}

fn file_family_snapshot(path: &Path) -> BTreeMap<String, Option<[u8; 32]>> {
    ["", "-wal", "-shm"]
        .into_iter()
        .map(|suffix| {
            let family_path = PathBuf::from(format!("{}{suffix}", path.display()));
            let hash = family_path.exists().then(|| {
                let bytes = std::fs::read(&family_path).unwrap();
                Sha256::digest(bytes).into()
            });
            (suffix.to_string(), hash)
        })
        .collect()
}

#[tokio::test]
async fn test_vfs_creation() {
    let vfs = Vfs::open(VfsOptions::ephemeral()).await.unwrap();
    // Just verify we can get the connection
    vfs.get_pool().execute(|_| Ok(())).await.unwrap();
}

#[tokio::test]
async fn open_read_only_preserves_single_file_artifact_family() {
    let temp_dir = tempfile::tempdir().unwrap();
    let artifact_path = create_frozen_artifact(temp_dir.path()).await;
    let before = file_family_snapshot(&artifact_path);

    {
        let vfs = Vfs::open_read_only(&artifact_path).await.unwrap();
        let stats = FileSystem::lookup(&vfs.fs, 1, "artifact.txt")
            .await
            .unwrap()
            .unwrap();
        let file = FileSystem::open(&vfs.fs, stats.ino, libc::O_RDONLY)
            .await
            .unwrap();
        assert_eq!(file.pread(0, 64).await.unwrap(), b"frozen artifact");
        assert_eq!(
            FileSystem::readdir(&vfs.fs, 1).await.unwrap().unwrap(),
            vec!["artifact.txt"]
        );
        FileSystem::finalize(&vfs.fs).await.unwrap();
    }

    assert_eq!(file_family_snapshot(&artifact_path), before);
    assert!(!PathBuf::from(format!("{}-wal", artifact_path.display())).exists());
    assert!(!PathBuf::from(format!("{}-shm", artifact_path.display())).exists());
}

#[tokio::test]
async fn open_read_only_rejects_filesystem_write_without_mutating_family() {
    let temp_dir = tempfile::tempdir().unwrap();
    let artifact_path = create_frozen_artifact(temp_dir.path()).await;
    let before = file_family_snapshot(&artifact_path);

    {
        let vfs = Vfs::open_read_only(&artifact_path).await.unwrap();
        let error = FileSystem::mkdir(&vfs.fs, 1, "forbidden", DEFAULT_DIR_MODE, 1000, 1000)
            .await
            .unwrap_err();
        assert!(
            matches!(error, Error::Database(tokio_rusqlite::rusqlite::Error::SqliteFailure(code, _)) if code.code == tokio_rusqlite::rusqlite::ErrorCode::ReadOnly),
            "unexpected write error: {error:?}"
        );
        FileSystem::drain_all(&vfs.fs).await.unwrap();
    }

    assert_eq!(file_family_snapshot(&artifact_path), before);
}

#[cfg(unix)]
#[tokio::test]
async fn open_read_only_reads_chmod_0444_artifact() {
    let temp_dir = tempfile::tempdir().unwrap();
    let artifact_path = create_frozen_artifact(temp_dir.path()).await;
    std::fs::set_permissions(&artifact_path, std::fs::Permissions::from_mode(0o444)).unwrap();
    let before = file_family_snapshot(&artifact_path);

    {
        let vfs = Vfs::open_read_only(&artifact_path).await.unwrap();
        let stats = FileSystem::lookup(&vfs.fs, 1, "artifact.txt")
            .await
            .unwrap()
            .unwrap();
        let file = FileSystem::open(&vfs.fs, stats.ino, libc::O_RDONLY)
            .await
            .unwrap();
        assert_eq!(file.pread(0, 64).await.unwrap(), b"frozen artifact");
    }

    assert_eq!(file_family_snapshot(&artifact_path), before);
}

#[tokio::test]
async fn test_filesystem_operations() {
    let vfs = Vfs::open(VfsOptions::ephemeral()).await.unwrap();

    // Create a directory
    vfs.fs.mkdir("/test_dir", 0, 0).await.unwrap();

    // Check directory exists
    let stats = vfs.fs.stat("/test_dir").await.unwrap();
    assert!(stats.is_some());
    let dir_stats = stats.unwrap();
    assert!(dir_stats.is_directory());

    // Write a file
    let data = b"Hello, Vfs!";
    let (_, file) = vfs
        .fs
        .create_file("/test_dir/test.txt", DEFAULT_FILE_MODE, 0, 0)
        .await
        .unwrap();
    file.pwrite(0, data).await.unwrap();

    // Read the file
    let read_data = vfs
        .fs
        .read_file("/test_dir/test.txt")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(read_data, data);

    // List directory
    let entries = vfs.fs.readdir(dir_stats.ino).await.unwrap().unwrap();
    assert_eq!(entries, vec!["test.txt"]);
}

#[test]
fn test_db_path_is_absolute() {
    // Mount teardown chdirs the process to `/`; a relative db path handed
    // to SQLite would make every later by-path operation resolve wrong.
    let by_path = VfsOptions::with_path("some-dir/relative.db")
        .db_path()
        .unwrap();
    assert!(
        std::path::Path::new(&by_path).is_absolute(),
        "with_path must absolutize: {by_path}"
    );
    assert!(std::path::Path::new(&by_path).ends_with("some-dir/relative.db"));

    assert_eq!(VfsOptions::ephemeral().db_path().unwrap(), ":memory:");
    assert_eq!(
        VfsOptions::with_path(":memory:").db_path().unwrap(),
        ":memory:"
    );
}
