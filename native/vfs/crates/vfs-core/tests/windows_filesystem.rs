//! Native Windows contracts, with bounded ordinary fixtures.
#![cfg(windows)]
use std::{path::Path, sync::Arc};
use tempfile::{tempdir, TempDir};
use vfs_core::fs::base_fingerprint::BaseFingerprint;
use vfs_core::{
    error::Result, FileSystem, HostFS, OverlayFS, PartialOriginMode, PartialOriginPolicy, Vfs,
    VfsOptions,
};
fn fixture() -> TempDir {
    tempdir().unwrap()
}
#[tokio::test]
async fn case_only_rename_preserves_requested_spelling_and_identity() -> Result<()> {
    let dir = fixture();
    let base = dir.path().join("base");
    std::fs::create_dir(&base)?;
    let bytes = vec![b'A'; 131072];
    std::fs::write(base.join("Mixed.bin"), &bytes)?;
    let db = dir.path().join("delta.db");
    let fs = overlay(&base, &db).await?;
    let source = fs.lookup(1, "Mixed.bin").await?.unwrap();
    let identity = fs.file_identity(source.ino)?;
    for spelling in ["MIXED.BIN", "mixed.bin", "Mixed.bin"] {
        fs.rename(1, "Mixed.bin", 1, spelling).await?;
        let entries = fs.readdir_plus(1).await?.unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].name, spelling);
        assert_eq!(entries[0].stats.ino, source.ino);
        assert_eq!(fs.file_identity(source.ino)?, identity);
        assert_eq!(
            fs.open(source.ino, libc::O_RDONLY)
                .await?
                .pread(0, 131072)
                .await?,
            bytes
        );
    }
    fs.finalize().await?;
    assert_eq!(std::fs::read(base.join("Mixed.bin"))?, bytes);
    Ok(())
}
#[tokio::test]
async fn replaced_partial_origin_is_removed_from_live_directory_enumeration() -> Result<()> {
    let dir = fixture();
    let base = dir.path().join("base");
    std::fs::create_dir(&base)?;
    std::fs::write(base.join("source.bin"), vec![b'S'; 131072])?;
    std::fs::write(base.join("target.bin"), vec![b'T'; 131072])?;
    let fs = overlay(&base, &dir.path().join("delta.db")).await?;
    for (name, patch) in [("source.bin", b"source"), ("target.bin", b"target")] {
        let stats = fs.lookup(1, name).await?.unwrap();
        let file = fs.open(stats.ino, libc::O_RDWR).await?;
        file.pwrite(0, patch).await?;
        file.fsync().await?;
    }
    fs.rename(1, "source.bin", 1, "target.bin").await?;
    let entries = fs.readdir_plus(1).await?.unwrap();
    assert_eq!(entries.len(), 1);
    assert_eq!(entries[0].name, "target.bin");
    assert_eq!(
        fs.open(entries[0].stats.ino, libc::O_RDONLY)
            .await?
            .pread(0, 6)
            .await?,
        b"source"
    );
    assert_eq!(std::fs::read(base.join("target.bin"))?, vec![b'T'; 131072]);
    fs.finalize().await?;
    Ok(())
}
async fn overlay(base: &Path, db: &Path) -> Result<OverlayFS> {
    let sdk = Vfs::open(VfsOptions::with_path(db.to_string_lossy())).await?;
    let fs = OverlayFS::new_with_partial_origin_policy(
        Arc::new(HostFS::new(base)?),
        sdk.fs,
        PartialOriginPolicy::new(PartialOriginMode::On),
    );
    fs.init(base.to_str().unwrap()).await?;
    Ok(fs)
}
#[tokio::test]
async fn host_startup_is_lazy_and_readonly() -> Result<()> {
    let dir = fixture();
    std::fs::write(dir.path().join("Data.txt"), b"original")?;
    let fs = HostFS::new(dir.path())?;
    assert_eq!(fs.observations()["directory_enumerations"], 0);
    assert_eq!(fs.observations()["data_read_bytes"], 0);
    let s = fs.lookup(1, "data.TXT").await?.unwrap();
    assert_eq!(
        fs.lookup_named(1, "DATA.txt").await?.unwrap().name,
        "Data.txt"
    );
    assert!(fs.open(s.ino, libc::O_RDWR).await.is_err());
    let f = fs.open(s.ino, libc::O_RDONLY).await?;
    assert!(f.pwrite(0, b"changed").await.is_err());
    assert_eq!(f.pread(0, 32).await?, b"original");
    assert_eq!(
        BaseFingerprint::from_stats(&s),
        BaseFingerprint::from_path(&dir.path().join("Data.txt"))?
    );
    Ok(())
}
#[tokio::test]
async fn native_identity_survives_rename_hardlinks_and_lookup_order() -> Result<()> {
    let dir = fixture();
    std::fs::write(dir.path().join("a"), b"first")?;
    std::fs::write(dir.path().join("z"), b"second")?;
    std::fs::hard_link(dir.path().join("a"), dir.path().join("alias"))?;
    let fs = HostFS::new(dir.path())?;
    let a = fs.lookup(1, "a").await?.unwrap();
    let id = fs.file_identity(a.ino)?;
    let alias = fs.lookup(1, "alias").await?.unwrap();
    assert_eq!(a.ino, alias.ino);
    std::fs::rename(dir.path().join("a"), dir.path().join("moved"))?;
    assert_eq!(
        fs.file_identity(fs.lookup(1, "moved").await?.unwrap().ino)?,
        id
    );
    let reopened = HostFS::new(dir.path())?;
    reopened.lookup(1, "z").await?;
    let moved = reopened.lookup(1, "moved").await?.unwrap();
    assert_ne!(moved.ino, a.ino);
    assert_eq!(reopened.file_identity(moved.ino)?, id);
    std::fs::write(dir.path().join("replacement"), b"first")?;
    assert_ne!(
        reopened.file_identity(reopened.lookup(1, "replacement").await?.unwrap().ino)?,
        id
    );
    Ok(())
}
#[tokio::test]
async fn origin_mapping_reopens_with_a_different_base_inode_allocation() -> Result<()> {
    let dir = fixture();
    let base = dir.path().join("base");
    std::fs::create_dir(&base)?;
    let db = dir.path().join("delta.db");
    let bytes = vec![b'A'; 131072];
    std::fs::write(base.join("Original.bin"), &bytes)?;
    std::fs::hard_link(base.join("Original.bin"), base.join("alias.bin"))?;
    std::fs::write(base.join("other"), b"other")?;
    let fs = overlay(&base, &db).await?;
    let a = fs.lookup(1, "original.BIN").await?.unwrap();
    let id = fs.file_identity(a.ino)?;
    let f = fs.open(a.ino, libc::O_RDWR).await?;
    f.pwrite(7, b"XYZ").await?;
    f.fsync().await?;
    assert_eq!(fs.file_identity(a.ino)?, id);
    fs.rename(1, "ALIAS.bin", 1, "renamed.bin").await?;
    assert_eq!(std::fs::read(base.join("Original.bin"))?, bytes);
    drop(f);
    fs.finalize().await?;
    drop(fs);
    let host = Arc::new(HostFS::new(&base)?);
    host.lookup(1, "other").await?;
    let sdk = Vfs::open(VfsOptions::with_path(db.to_string_lossy())).await?;
    let fs = OverlayFS::new_with_partial_origin_policy(
        host,
        sdk.fs,
        PartialOriginPolicy::new(PartialOriginMode::On),
    );
    fs.init(base.to_str().unwrap()).await?;
    let alias = fs.lookup(1, "renamed.BIN").await?.unwrap();
    let original = fs.lookup(1, "Original.bin").await?.unwrap();
    assert_eq!(alias.ino, original.ino);
    assert_eq!(fs.file_identity(original.ino)?, id);
    assert_eq!(
        fs.open(alias.ino, libc::O_RDONLY)
            .await?
            .pread(7, 3)
            .await?,
        b"XYZ"
    );
    assert!(fs.lookup(1, "alias.BIN").await?.is_none());
    Ok(())
}
#[tokio::test]
async fn full_chunk_overwrite_does_not_read_base_bytes() -> Result<()> {
    let dir = fixture();
    let base = dir.path().join("base");
    std::fs::create_dir(&base)?;
    std::fs::write(base.join("large"), vec![b'A'; 131072])?;
    let sdk = Vfs::open(VfsOptions::with_path(
        dir.path().join("delta.db").to_string_lossy(),
    ))
    .await?;
    let chunk = sdk.fs.chunk_size();
    let host = Arc::new(HostFS::new(&base)?);
    let fs = OverlayFS::new_with_partial_origin_policy(
        host.clone(),
        sdk.fs,
        PartialOriginPolicy::new(PartialOriginMode::On),
    );
    fs.init(base.to_str().unwrap()).await?;
    let s = fs.lookup(1, "large").await?.unwrap();
    let f = fs.open(s.ino, libc::O_RDWR).await?;
    f.pwrite(0, &vec![b'B'; chunk]).await?;
    assert_eq!(host.observations()["data_read_bytes"], 0);
    assert_eq!(f.pread(0, 1).await?, b"B");
    assert_eq!(f.pread(chunk as u64, 1).await?, b"A");
    Ok(())
}
#[tokio::test]
async fn partial_handle_keeps_unlinked_inode_until_close() -> Result<()> {
    let dir = fixture();
    let base = dir.path().join("base");
    std::fs::create_dir(&base)?;
    std::fs::write(base.join("file"), vec![b'A'; 131072])?;
    let fs = overlay(&base, &dir.path().join("delta.db")).await?;
    let s = fs.lookup(1, "file").await?.unwrap();
    let f = fs.open(s.ino, libc::O_RDWR).await?;
    f.pwrite(0, b"X").await?;
    fs.unlink(1, "file").await?;
    assert!(fs.lookup(1, "file").await?.is_none());
    f.pwrite(1, b"Y").await?;
    assert_eq!(f.pread(0, 2).await?, b"XY");
    f.fsync().await?;
    assert_eq!(&std::fs::read(base.join("file"))?[..2], b"AA");
    Ok(())
}
#[tokio::test]
async fn delta_names_and_whiteouts_follow_windows_ordinal_semantics() -> Result<()> {
    let dir = fixture();
    let base = dir.path().join("base");
    std::fs::create_dir(&base)?;
    std::fs::write(base.join("MiXeD"), b"base")?;
    let fs = overlay(&base, &dir.path().join("delta.db")).await?;
    fs.unlink(1, "mixed").await?;
    assert!(fs.lookup(1, "MIXED").await?.is_none());
    let (_, f) = fs.create_file(1, "Created", 0o644, 0, 0).await?;
    f.pwrite(0, b"private").await?;
    assert!(fs.create_file(1, "cREATED", 0o644, 0, 0).await.is_err());
    assert_eq!(
        fs.lookup_named(1, "CREATED").await?.unwrap().name,
        "Created"
    );
    assert!(!base.join("Created").exists());
    Ok(())
}
#[tokio::test]
async fn current_format_refuses_old_or_malformed_origins_without_repair() -> Result<()> {
    let dir = fixture();
    let path = dir.path().join("schema.db");
    let sdk = Vfs::open(VfsOptions::with_path(path.to_string_lossy())).await?;
    let conn = sdk.get_connection().await?;
    conn.execute("PRAGMA user_version = 8", ()).await?;
    assert!(matches!(
        vfs_core::schema::ensure_current(&conn).await,
        Err(vfs_core::error::Error::SchemaVersionMismatch { .. })
    ));
    let mut rows = conn.query("PRAGMA user_version", ()).await?;
    assert_eq!(rows.next().await?.unwrap().get::<i64>(0)?, 8);
    drop(rows);
    conn.execute(
        &format!(
            "PRAGMA user_version = {}",
            vfs_core::schema::CURRENT.user_version()
        ),
        (),
    )
    .await?;
    conn.execute(
        "ALTER TABLE fs_origin RENAME COLUMN base_identity TO base_ino",
        (),
    )
    .await?;
    assert!(vfs_core::schema::ensure_current(&conn).await.is_err());
    Ok(())
}

struct ContentHash {
    path: std::path::PathBuf,
    digest: blake3::Hash,
}

#[tokio::test]
async fn every_overlay_handle_reports_the_visible_inode() -> Result<()> {
    for mode in [PartialOriginMode::Off, PartialOriginMode::On] {
        let dir = fixture();
        let base = dir.path().join("base");
        std::fs::create_dir(&base)?;
        std::fs::write(base.join("first"), vec![b'A'; 131072])?;
        std::fs::write(base.join("second"), b"second")?;
        let sdk = Vfs::open(VfsOptions::with_path(
            dir.path().join("delta.db").to_string_lossy(),
        ))
        .await?;
        let fs = OverlayFS::new_with_partial_origin_policy(
            Arc::new(HostFS::new(&base)?),
            sdk.fs,
            PartialOriginPolicy::new(mode),
        );
        fs.init(base.to_str().unwrap()).await?;
        // Allocate visible base inodes before allocating any delta inode.
        let first = fs.lookup(1, "first").await?.unwrap();
        fs.lookup(1, "second").await?.unwrap();
        let (created, file) = fs.create_file(1, "private", 0o644, 0, 0).await?;
        assert_eq!(file.fstat().await?.ino, created.ino);
        assert_eq!(
            fs.open(created.ino, libc::O_RDWR).await?.fstat().await?.ino,
            created.ino
        );
        let copied = fs.open(first.ino, libc::O_RDWR).await?;
        copied.pwrite(0, b"X").await?;
        assert_eq!(copied.fstat().await?.ino, first.ino);
        assert_eq!(
            fs.open(first.ino, libc::O_RDONLY).await?.fstat().await?.ino,
            first.ino
        );
        drop(copied);
        drop(file);
        fs.finalize().await?;
    }
    Ok(())
}
impl vfs_core::fs::BaseValidator for ContentHash {
    fn validate(&self, _: &dyn FileSystem, _: &vfs_core::Stats) -> Result<()> {
        if blake3::hash(&std::fs::read(&self.path)?) != self.digest {
            return Err(vfs_core::error::Error::Internal(
                "base content hash changed".into(),
            ));
        }
        Ok(())
    }
}
#[tokio::test]
async fn content_policy_allows_time_drift_but_cannot_replace_identity() -> Result<()> {
    let dir = fixture();
    let base = dir.path().join("base");
    std::fs::create_dir(&base)?;
    let path = base.join("data");
    let original = vec![b'A'; 131072];
    std::fs::write(&path, &original)?;
    let sdk = Vfs::open(VfsOptions::with_path(
        dir.path().join("delta.db").to_string_lossy(),
    ))
    .await?;
    let fs = OverlayFS::new_with_partial_origin_policy(
        Arc::new(HostFS::new(&base)?),
        sdk.fs,
        PartialOriginPolicy::new(PartialOriginMode::On),
    )
    .with_base_validator(Arc::new(ContentHash {
        path: path.clone(),
        digest: blake3::hash(&original),
    }));
    fs.init(base.to_str().unwrap()).await?;
    let stats = fs.lookup(1, "data").await?.unwrap();
    let f = fs.open(stats.ino, libc::O_RDWR).await?;
    f.pwrite(0, b"X").await?;
    std::fs::write(&path, &original)?;
    assert_eq!(f.pread(0, 2).await?, b"XA");
    let replacement = base.join("replacement");
    std::fs::write(&replacement, &original)?;
    std::fs::rename(&path, base.join("old"))?;
    std::fs::rename(&replacement, &path)?;
    assert!(matches!(
        f.pread(0, 2).await,
        Err(vfs_core::error::Error::Fs(vfs_core::FsError::Corrupt(_)))
    ));
    Ok(())
}
