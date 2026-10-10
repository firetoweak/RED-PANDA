//! Persistent identity contracts, independent of mount transport and command policy.
use std::path::Path;
use std::sync::Arc;
use tempfile::tempdir;
use tokio_rusqlite::rusqlite::Connection;
use vfs_core::{
    error::{Error, Result},
    FileSystem, Vfs, VfsOptions,
};
use vfs_core::{OverlayFS, PartialOriginMode, PartialOriginPolicy};

#[tokio::test]
async fn three_stacked_deltas_preserve_independent_files_and_inherited_aliases() -> Result<()> {
    for mode in [PartialOriginMode::Off, PartialOriginMode::On] {
        let dir = tempdir()?;
        let a = open(&dir.path().join("a.db")).await?;
        let (p, file) = FileSystem::create_file(&a.fs, 1, "parent", 0o644, 0, 0).await?;
        file.pwrite(0, &vec![b'P'; 131072]).await?;
        file.fsync().await?;
        drop(file);
        a.fs.link(p.ino, 1, "alias").await?;
        let parent_id = a.fs.file_identity(p.ino)?;
        let a_image = dir.path().join("a-image.db");
        a.snapshot_into(&a_image).await?;
        let a_read = Vfs::open_read_only(&a_image).await?;
        let b = open(&dir.path().join("b.db")).await?;
        let b_view = OverlayFS::new_with_partial_origin_policy(
            Arc::new(a_read.fs.clone()),
            b.fs.clone(),
            PartialOriginPolicy::new(mode),
        );
        b_view.init("artifact://a").await?;
        let (q, file) = FileSystem::create_file(&b.fs, 1, "child", 0o644, 0, 0).await?;
        file.pwrite(0, &vec![b'Q'; 131072]).await?;
        file.fsync().await?;
        drop(file);
        assert_eq!(p.ino, q.ino);
        let child_id = b.fs.file_identity(q.ino)?;
        assert_ne!(parent_id, child_id);
        b_view.finalize().await?;
        let b_image = dir.path().join("b-image.db");
        b.snapshot_into(&b_image).await?;
        let b_read = Vfs::open_read_only(&b_image).await?;
        let parent_view = Arc::new(OverlayFS::new_with_partial_origin_policy(
            Arc::new(a_read.fs.clone()),
            b_read.fs.clone(),
            PartialOriginPolicy::new(mode),
        ));
        parent_view.load().await?;
        let c = open(&dir.path().join("c.db")).await?;
        let c_view = OverlayFS::new_with_partial_origin_policy(
            parent_view.clone(),
            c.fs.clone(),
            PartialOriginPolicy::new(mode),
        );
        c_view.init("artifact://b").await?;
        let parent = c_view.lookup(1, "parent").await?.unwrap();
        let file = c_view.open(parent.ino, libc::O_RDWR).await?;
        file.pwrite(70007, b"NEW").await?;
        file.fsync().await?;
        drop(file);
        let mut changed = vec![b'P'; 131072];
        changed[70007..70010].copy_from_slice(b"NEW");
        c_view.finalize().await?;
        let c_image = dir.path().join("c-image.db");
        c.snapshot_into(&c_image).await?;
        let c_read = Vfs::open_read_only(&c_image).await?;
        let restored = OverlayFS::new_with_partial_origin_policy(
            parent_view,
            c_read.fs.clone(),
            PartialOriginPolicy::new(mode),
        );
        restored.load().await?;
        for fs in [&c_view as &dyn FileSystem, &restored as &dyn FileSystem] {
            for name in ["parent", "alias"] {
                let stats = fs.lookup(1, name).await?.unwrap();
                assert_eq!(fs.file_identity(stats.ino)?, parent_id);
                assert_eq!(
                    fs.open(stats.ino, libc::O_RDONLY)
                        .await?
                        .pread(0, 131072)
                        .await?,
                    changed
                );
            }
            let child = fs.lookup(1, "child").await?.unwrap();
            assert_eq!(fs.file_identity(child.ino)?, child_id);
            assert_eq!(
                fs.open(child.ino, libc::O_RDONLY)
                    .await?
                    .pread(0, 131072)
                    .await?,
                vec![b'Q'; 131072]
            );
        }
    }
    Ok(())
}

async fn open(path: &Path) -> Result<Vfs> {
    Vfs::open(VfsOptions::with_path(path.to_string_lossy())).await
}

#[tokio::test]
async fn missing_name_lookup_through_32_layers_does_not_repeat_base_traversal() -> Result<()> {
    let dir = tempdir()?;
    let base = open(&dir.path().join("base.db")).await?;
    let mut view: Arc<dyn FileSystem> = Arc::new(base.fs.clone());
    for depth in 0..32 {
        let delta = open(&dir.path().join(format!("delta-{depth}.db"))).await?;
        let overlay = OverlayFS::new(view, delta.fs.clone());
        overlay.init("artifact://base").await?;
        view = Arc::new(overlay);
    }
    let missing = tokio::time::timeout(
        std::time::Duration::from_secs(10),
        view.lookup_named(1, "absent"),
    )
    .await
    .expect("missing-name traversal must remain bounded across stacked layers")?;
    assert!(missing.is_none());
    Ok(())
}

#[tokio::test]
async fn named_lookup_preserves_distinct_hardlink_entry_spellings() -> Result<()> {
    let dir = tempdir()?;
    let base = open(&dir.path().join("base.db")).await?;
    let (file, handle) = FileSystem::create_file(&base.fs, 1, "file", 0o644, 0, 0).await?;
    drop(handle);
    base.fs.link(file.ino, 1, "alias").await?;
    let mut view: Arc<dyn FileSystem> = Arc::new(base.fs.clone());
    for depth in 0..3 {
        let delta = open(&dir.path().join(format!("delta-{depth}.db"))).await?;
        let overlay = OverlayFS::new(view, delta.fs.clone());
        overlay.init("artifact://base").await?;
        view = Arc::new(overlay);
        for name in ["file", "alias", "file"] {
            assert_eq!(view.lookup_named(1, name).await?.unwrap().name, name);
        }
    }
    Ok(())
}

#[tokio::test]
async fn separate_deltas_and_equal_content_files_keep_distinct_identity() -> Result<()> {
    let dir = tempdir()?;
    let a = open(&dir.path().join("a.db")).await?;
    let b = open(&dir.path().join("b.db")).await?;
    let (first, f) = FileSystem::create_file(&a.fs, 1, "first", 0o644, 0, 0).await?;
    f.pwrite(0, b"same bytes").await?;
    f.fsync().await?;
    let (second, f) = FileSystem::create_file(&a.fs, 1, "second", 0o644, 0, 0).await?;
    f.pwrite(0, b"same bytes").await?;
    f.fsync().await?;
    let (other, f) = FileSystem::create_file(&b.fs, 1, "other", 0o644, 0, 0).await?;
    f.pwrite(0, b"same bytes").await?;
    f.fsync().await?;
    assert_eq!(first.ino, other.ino, "independent DBs reuse inode numbers");
    assert_ne!(
        a.fs.file_identity(first.ino)?,
        b.fs.file_identity(other.ino)?
    );
    assert_ne!(
        a.fs.file_identity(first.ino)?,
        a.fs.file_identity(second.ino)?
    );
    let alias = a.fs.link(first.ino, 1, "alias").await?;
    assert_eq!(
        a.fs.file_identity(first.ino)?,
        a.fs.file_identity(alias.ino)?
    );
    Ok(())
}

#[tokio::test]
async fn identity_survives_snapshot_path_changes_and_writable_copy() -> Result<()> {
    let dir = tempdir()?;
    let sdk = open(&dir.path().join("writer.db")).await?;
    let (stats, f) = FileSystem::create_file(&sdk.fs, 1, "original", 0o644, 0, 0).await?;
    f.pwrite(0, b"original bytes").await?;
    f.fsync().await?;
    drop(f);
    let id = sdk.fs.file_identity(stats.ino)?;
    sdk.fs.rename(1, "original", 1, "renamed").await?;
    assert_eq!(sdk.fs.file_identity(stats.ino)?, id);
    let image = dir.path().join("immutable.db");
    sdk.snapshot_into(&image).await?;
    let relocated = dir.path().join("published.db");
    std::fs::rename(&image, &relocated)?;
    let reader = Vfs::open_read_only(&relocated).await?;
    let renamed = reader.fs.lookup(1, "renamed").await?.unwrap();
    assert_eq!(reader.fs.file_identity(renamed.ino)?, id);
    let staging = dir.path().join("writable-copy.db");
    sdk.snapshot_into(&staging).await?;
    let restored = open(&staging).await?;
    assert!(restored.fs.lookup(1, "original").await?.is_none());
    let original = restored.fs.lookup(1, "renamed").await?.unwrap();
    assert_eq!(restored.fs.file_identity(original.ino)?, id);
    assert_eq!(
        FileSystem::open(&restored.fs, original.ino, libc::O_RDONLY)
            .await?
            .pread(0, 64)
            .await?,
        b"original bytes"
    );
    Ok(())
}

#[tokio::test]
async fn missing_or_malformed_namespace_is_refused_without_regeneration() -> Result<()> {
    for invalid in [
        None,
        Some("not-a-uuid"),
        Some("00000000-0000-0000-0000-000000000000"),
    ] {
        let dir = tempdir()?;
        let path = dir.path().join("bad.db");
        let sdk = open(&path).await?;
        let conn = Connection::open(&path)?;
        if let Some(value) = invalid {
            conn.execute(
                "UPDATE fs_config SET value = ? WHERE key = 'filesystem_id'",
                (value,),
            )?;
        } else {
            conn.execute("DELETE FROM fs_config WHERE key = 'filesystem_id'", ())?;
        }
        assert!(matches!(
            vfs_core::schema::ensure_current(&conn),
            Err(Error::Internal(_))
        ));
        let error = open(&path)
            .await
            .err()
            .expect("writable open accepted corrupt identity");
        assert!(matches!(error, Error::Internal(_)), "{error}");
        let image = dir.path().join("bad-artifact.db");
        sdk.snapshot_into(&image).await?;
        let error = Vfs::open_read_only(&image)
            .await
            .err()
            .expect("read-only open accepted corrupt identity");
        assert!(matches!(error, Error::Internal(_)), "{error}");
        let mut statement_0 =
            conn.prepare("SELECT value FROM fs_config WHERE key = 'filesystem_id'")?;
        let mut rows = statement_0.query(())?;
        let persisted = match rows.next()? {
            Some(row) => Some(row.get::<_, String>(0)?),
            None => None,
        };
        assert_eq!(
            persisted.as_deref(),
            invalid,
            "open regenerated a missing or corrupt namespace"
        );
    }
    Ok(())
}

#[tokio::test]
async fn old_unnamespaced_format_is_refused_without_rewriting_it() -> Result<()> {
    let dir = tempdir()?;
    let path = dir.path().join("old.db");
    let sdk = open(&path).await?;
    let conn = Connection::open(&path)?;
    conn.execute("PRAGMA user_version = 9", ())?;
    conn.execute(
        "UPDATE fs_config SET value = '0.9' WHERE key = 'schema_version'",
        (),
    )?;
    conn.execute("DELETE FROM fs_config WHERE key = 'filesystem_id'", ())?;
    assert!(matches!(
        vfs_core::schema::ensure_current(&conn),
        Err(Error::SchemaVersionMismatch { .. })
    ));
    assert!(matches!(
        open(&path).await.err().unwrap(),
        Error::SchemaVersionMismatch { .. }
    ));
    let image = dir.path().join("old-artifact.db");
    sdk.snapshot_into(&image).await?;
    assert!(matches!(
        Vfs::open_read_only(&image).await.err().unwrap(),
        Error::SchemaVersionMismatch { .. }
    ));
    let mut statement_1 = conn.prepare("PRAGMA user_version")?;
    let mut rows = statement_1.query(())?;
    assert_eq!(rows.next()?.unwrap().get::<_, i64>(0)?, 9);
    drop(rows);
    let mut statement_2 =
        conn.prepare("SELECT COUNT(*) FROM fs_config WHERE key = 'filesystem_id'")?;
    let mut rows = statement_2.query(())?;
    assert_eq!(rows.next()?.unwrap().get::<_, i64>(0)?, 0);
    Ok(())
}
