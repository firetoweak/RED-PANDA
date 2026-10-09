#![cfg(all(windows, feature = "winfsp"))]
use anyhow::{Context, Result};
use std::{
    io::{Read, Seek, SeekFrom, Write},
    os::windows::{ffi::OsStrExt, io::AsRawHandle},
    path::Path,
    sync::Arc,
};
use vfs_core::{
    schema::integrity::{check, CheckOpts},
    FileSystem, HostFS, OverlayFS, PartialOriginMode, PartialOriginPolicy, Vfs, VfsOptions,
};
use vfs_mount::{mount_fs, Backend, MountOpts};

#[link(name = "kernel32")]
unsafe extern "system" {
    fn MoveFileExW(from: *const u16, to: *const u16, flags: u32) -> i32;
    fn GetFinalPathNameByHandleW(
        file: *mut std::ffi::c_void,
        path: *mut u16,
        size: u32,
        flags: u32,
    ) -> u32;
}
fn normalized_name(file: &std::fs::File) -> std::io::Result<String> {
    // SAFETY: the handle is owned by file; the first call measures the NT path buffer.
    let size =
        unsafe { GetFinalPathNameByHandleW(file.as_raw_handle(), std::ptr::null_mut(), 0, 2) };
    if size == 0 {
        return Err(std::io::Error::last_os_error());
    }
    let mut path = vec![0; size as usize];
    // SAFETY: this buffer has the measured capacity; the file remains open throughout.
    let size =
        unsafe { GetFinalPathNameByHandleW(file.as_raw_handle(), path.as_mut_ptr(), size, 2) };
    if size == 0 {
        return Err(std::io::Error::last_os_error());
    }
    assert!((size as usize) < path.len());
    Ok(String::from_utf16(&path[..size as usize]).unwrap())
}
fn rename_without_replacement(from: &Path, to: &Path) -> std::io::Result<()> {
    let from: Vec<_> = from.as_os_str().encode_wide().chain(Some(0)).collect();
    let to: Vec<_> = to.as_os_str().encode_wide().chain(Some(0)).collect();
    // SAFETY: both paths are live, NUL-terminated buffers for this synchronous call.
    if unsafe { MoveFileExW(from.as_ptr(), to.as_ptr(), 0) } == 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}
const REPLACEMENTS: [(&str, &str, bool, bool); 5] = [
    ("base-base", "base-base-target.bin", true, true),
    (
        "base-delta",
        "existing/new/nested/base-delta-target.bin",
        true,
        false,
    ),
    ("delta-base", "delta-base-target.bin", false, true),
    ("delta-delta", "delta-delta-target.bin", false, false),
    ("partial-partial", "partial-partial-target.bin", true, true),
];
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "requires installed WinFsp runtime and SDK build"]
async fn mounted_native_io_is_private_and_survives_reopen() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let base = dir.path().join("base");
    std::fs::create_dir(&base)?;
    let original = vec![b'A'; 131072];
    std::fs::write(base.join("Original.bin"), &original)?;
    std::fs::hard_link(base.join("Original.bin"), base.join("alias.bin"))?;
    std::fs::create_dir(base.join("existing"))?;
    std::fs::write(base.join("existing/sentinel.txt"), b"host unchanged")?;
    std::fs::write(base.join("BaSeCase.bin"), b"base case unchanged")?;
    for (case, target, base_source, base_target) in REPLACEMENTS {
        if base_source {
            std::fs::write(base.join(format!("{case}-source.bin")), vec![b'S'; 131072])?;
        }
        if base_target {
            std::fs::write(base.join(target), vec![b'T'; 131072])?;
        }
    }
    let db = dir.path().join("delta.db");
    let sdk = Vfs::open(VfsOptions::with_path(db.to_string_lossy())).await?;
    let view = Arc::new(OverlayFS::new_with_partial_origin_policy(
        Arc::new(HostFS::new(&base)?),
        sdk.fs,
        PartialOriginPolicy::new(PartialOriginMode::On),
    ));
    view.init(base.to_str().unwrap()).await?;
    let mountpoint = dir.path().join("mounted");
    let handle = mount_fs(
        view.clone(),
        MountOpts::new(mountpoint.clone(), Backend::WinFsp),
    )
    .await?;
    let point = mountpoint.clone();
    let operations = tokio::task::spawn_blocking(move || -> Result<()> {
        assert_eq!(
            std::fs::read(point.join("ORIGINAL.BIN")).context("native initial read")?,
            original
        );
        let mut file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(point.join("original.bin"))?;
        file.seek(SeekFrom::Start(7))?;
        file.write_all(b"XYZ")?;
        file.sync_all().context("native flush")?;
        drop(file);
        let mut alias = std::fs::File::open(point.join("alias.bin"))?;
        alias.seek(SeekFrom::Start(7))?;
        let mut data = [0; 3];
        alias.read_exact(&mut data)?;
        assert_eq!(&data, b"XYZ");
        drop(alias);
        std::fs::rename(point.join("alias.bin"), point.join("renamed.bin"))
            .context("native rename to absent destination")?;
        std::fs::write(point.join("created.txt"), b"created privately")
            .context("native creation")?;
        assert_eq!(
            std::fs::read(point.join("CREATED.TXT"))?,
            b"created privately"
        );
        let names = std::fs::read_dir(&point)?
            .map(|e| e.unwrap().file_name())
            .collect::<Vec<_>>();
        assert!(names.contains(&"created.txt".into()));
        std::fs::remove_file(point.join("created.txt")).context("native deletion")?;

        let nested = point.join("existing/new/nested");
        std::fs::create_dir_all(&nested).context("native nested directory creation")?;
        std::fs::create_dir_all(&nested).context("native repeated create_dir_all")?;
        assert!(std::fs::metadata(&nested)?.is_dir());
        let duplicate = std::fs::create_dir(point.join("EXISTING/NEW/NESTED")).unwrap_err();
        assert_eq!(duplicate.kind(), std::io::ErrorKind::AlreadyExists);
        let missing_parent = std::fs::create_dir(point.join("missing/child")).unwrap_err();
        assert_eq!(missing_parent.kind(), std::io::ErrorKind::NotFound);
        let mut child = std::fs::File::create(nested.join("child.txt"))?;
        child.write_all(b"nested privately")?;
        child.sync_all()?;
        drop(child);
        assert_eq!(
            std::fs::read(nested.join("CHILD.TXT"))?,
            b"nested privately"
        );
        let names = std::fs::read_dir(point.join("existing/new"))?
            .map(|e| e.unwrap().file_name())
            .collect::<Vec<_>>();
        assert_eq!(names, vec![std::ffi::OsString::from("nested")]);
        let nonempty = std::fs::remove_dir(&nested).unwrap_err();
        assert_eq!(nonempty.raw_os_error(), Some(145));

        let removed = point.join("removed/inner");
        std::fs::create_dir_all(&removed)?;
        std::fs::write(removed.join("temporary.txt"), b"temporary")?;
        std::fs::remove_file(removed.join("temporary.txt"))?;
        std::fs::remove_dir(&removed).context("native empty child directory deletion")?;
        std::fs::remove_dir(point.join("removed")).context("native empty parent deletion")?;
        assert_eq!(
            std::fs::metadata(point.join("removed")).unwrap_err().kind(),
            std::io::ErrorKind::NotFound
        );

        std::fs::write(point.join("protected.txt"), b"keep source")?;
        let directory_target = std::fs::rename(point.join("protected.txt"), &nested).unwrap_err();
        assert_eq!(directory_target.raw_os_error(), Some(5));
        assert_eq!(std::fs::read(point.join("protected.txt"))?, b"keep source");
        assert_eq!(
            std::fs::read(nested.join("child.txt"))?,
            b"nested privately"
        );
        std::fs::remove_file(point.join("protected.txt"))?;

        for (case, target, base_source, base_target) in REPLACEMENTS {
            let source = point.join(format!("{case}-source.bin"));
            let target = point.join(target);
            if !base_source {
                std::fs::write(&source, vec![b'S'; 131072])?;
            }
            if !base_target {
                std::fs::write(&target, vec![b'T'; 131072])?;
            }
            let partial = case == "partial-partial";
            let mut source_handle = std::fs::OpenOptions::new()
                .read(true)
                .write(partial)
                .open(&source)?;
            let mut expected = vec![b'S'; 131072];
            let mut previous = vec![b'T'; 131072];
            if partial {
                source_handle.write_all(b"source patch")?;
                source_handle.sync_all()?;
                expected[..12].copy_from_slice(b"source patch");
                let mut target_handle = std::fs::OpenOptions::new().write(true).open(&target)?;
                target_handle.write_all(b"target patch")?;
                target_handle.sync_all()?;
                drop(target_handle);
                previous[..12].copy_from_slice(b"target patch");
            }
            let collision = rename_without_replacement(&source, &target).unwrap_err();
            assert_eq!(collision.raw_os_error(), Some(183), "{case}: collision");
            let held_target = std::fs::File::open(&target)?;
            let busy = std::fs::rename(&source, &target).unwrap_err();
            assert_eq!(busy.raw_os_error(), Some(5), "{case}: open target");
            assert_eq!(
                std::fs::read(&source)?,
                expected,
                "{case}: source after refusal"
            );
            assert_eq!(
                std::fs::read(&target)?,
                previous,
                "{case}: target after refusal"
            );
            drop(held_target);
            std::fs::rename(&source, &target).with_context(|| format!("{case}: replacement"))?;
            assert_eq!(std::fs::read(&target)?, expected, "{case}: replaced bytes");
            assert_eq!(
                std::fs::metadata(&source).unwrap_err().kind(),
                std::io::ErrorKind::NotFound
            );
            source_handle.seek(SeekFrom::Start(0))?;
            let mut opened = Vec::new();
            source_handle.read_to_end(&mut opened)?;
            assert_eq!(opened, expected, "{case}: source handle after rename");
            if partial {
                source_handle.seek(SeekFrom::Start(0))?;
                source_handle.write_all(b"after rename")?;
                source_handle.sync_all()?;
                expected[..12].copy_from_slice(b"after rename");
                assert_eq!(std::fs::read(&target)?, expected);
            }
            drop(source_handle);
        }
        let names = std::fs::read_dir(&point)?
            .map(|e| e.unwrap().file_name())
            .collect::<Vec<_>>();
        assert!(names.contains(&"partial-partial-target.bin".into()));
        assert!(!names.contains(&"partial-partial-source.bin".into()));
        for (initial, upper, final_name) in [
            (
                point.join("delta-delta-target.bin"),
                point.join("DELTA-DELTA-TARGET.BIN"),
                point.join("Delta-Delta-Target.bin"),
            ),
            (
                point.join("BaSeCase.bin"),
                point.join("BASECASE.BIN"),
                point.join("basecase.bin"),
            ),
            (
                nested.join("MiXeD_文件_😀.txt"),
                nested.join("MIXED_文件_😀.TXT"),
                nested.join("mixed_文件_😀.txt"),
            ),
        ] {
            if initial.file_name().unwrap() == "MiXeD_文件_😀.txt" {
                std::fs::write(&initial, b"unicode case unchanged")?;
            }
            let expected = std::fs::read(&initial)?;
            let file = std::fs::File::open(&initial)?;
            assert!(normalized_name(&file)?.ends_with(&format!(
                "\\{}",
                initial.file_name().unwrap().to_str().unwrap()
            )));
            drop(file);
            std::fs::rename(&initial, &upper)
                .context("native case-only rename with replacement")?;
            let file = std::fs::File::open(&initial)?;
            assert!(normalized_name(&file)?.ends_with(&format!(
                "\\{}",
                upper.file_name().unwrap().to_str().unwrap()
            )));
            drop(file);
            rename_without_replacement(&upper, &final_name)
                .context("native case-only rename without replacement")?;
            rename_without_replacement(&final_name, &final_name)
                .context("native identical-name rename")?;
            let names = std::fs::read_dir(final_name.parent().unwrap())?
                .map(|e| e.unwrap().file_name())
                .collect::<Vec<_>>();
            assert!(names.contains(&final_name.file_name().unwrap().to_os_string()));
            assert!(!names.contains(&upper.file_name().unwrap().to_os_string()));
            assert_eq!(std::fs::read(&initial)?, expected);
            let file = std::fs::File::open(&upper)?;
            let suffix = if final_name.parent().unwrap() == nested {
                format!(
                    "\\existing\\new\\nested\\{}",
                    final_name.file_name().unwrap().to_str().unwrap()
                )
            } else {
                format!("\\{}", final_name.file_name().unwrap().to_str().unwrap())
            };
            assert!(normalized_name(&file)?.ends_with(&suffix));
            drop(file);
        }
        Ok(())
    })
    .await;
    // Always use explicit teardown, including a failing operation sequence.
    let teardown = handle.unmount().await;
    teardown.context("mount teardown")?;
    operations??;
    assert_eq!(&std::fs::read(base.join("Original.bin"))?[7..10], b"AAA");
    assert!(base.join("alias.bin").exists());
    assert!(!base.join("renamed.bin").exists());
    assert!(!base.join("created.txt").exists());
    assert!(!base.join("existing/new").exists());
    assert!(!base.join("removed").exists());
    assert_eq!(
        std::fs::read(base.join("existing/sentinel.txt"))?,
        b"host unchanged"
    );
    assert_eq!(
        std::fs::read(base.join("BaSeCase.bin"))?,
        b"base case unchanged"
    );
    assert!(!base.join("existing/new").exists());
    for (case, target, base_source, base_target) in REPLACEMENTS {
        if base_source {
            assert_eq!(
                std::fs::read(base.join(format!("{case}-source.bin")))?,
                vec![b'S'; 131072]
            );
        }
        if base_target {
            assert_eq!(std::fs::read(base.join(target))?, vec![b'T'; 131072]);
        }
    }
    drop(view);
    let sdk = Vfs::open(VfsOptions::with_path(db.to_string_lossy())).await?;
    let view = OverlayFS::new_with_partial_origin_policy(
        Arc::new(HostFS::new(&base)?),
        sdk.fs.clone(),
        PartialOriginPolicy::new(PartialOriginMode::On),
    );
    view.init(base.to_str().unwrap()).await?;
    let file = view.lookup(1, "renamed.bin").await?.unwrap();
    assert_eq!(
        view.open(file.ino, libc::O_RDONLY)
            .await?
            .pread(7, 3)
            .await?,
        b"XYZ"
    );
    assert!(view.lookup(1, "alias.bin").await?.is_none());
    assert!(view.lookup(1, "created.txt").await?.is_none());
    assert!(view.lookup(1, "removed").await?.is_none());
    let existing = view.lookup(1, "existing").await?.unwrap();
    let created = view.lookup(existing.ino, "new").await?.unwrap();
    let nested = view.lookup(created.ino, "nested").await?.unwrap();
    assert!(created.is_directory());
    assert!(nested.is_directory());
    let child = view.lookup(nested.ino, "child.txt").await?.unwrap();
    assert_eq!(
        view.open(child.ino, libc::O_RDONLY)
            .await?
            .pread(0, 100)
            .await?,
        b"nested privately"
    );
    for (case, target, _, _) in REPLACEMENTS {
        assert!(view
            .lookup(1, &format!("{case}-source.bin"))
            .await?
            .is_none());
        let (parent, name) = if case == "base-delta" {
            (nested.ino, "base-delta-target.bin")
        } else {
            (1, target)
        };
        let target = view.lookup(parent, name).await?.unwrap();
        let mut expected = vec![b'S'; 131072];
        if case == "partial-partial" {
            expected[..12].copy_from_slice(b"after rename");
        }
        assert_eq!(
            view.open(target.ino, libc::O_RDONLY)
                .await?
                .pread(0, 131072)
                .await?,
            expected
        );
    }
    assert_eq!(
        view.lookup_named(1, "delta-delta-target.bin")
            .await?
            .unwrap()
            .name,
        "Delta-Delta-Target.bin"
    );
    assert_eq!(
        view.lookup_named(1, "BaSeCase.bin").await?.unwrap().name,
        "basecase.bin"
    );
    let unicode = view
        .lookup_named(nested.ino, "MIXED_文件_😀.TXT")
        .await?
        .unwrap();
    assert_eq!(unicode.name, "mixed_文件_😀.txt");
    assert_eq!(
        view.open(unicode.stats.ino, libc::O_RDONLY)
            .await?
            .pread(0, 100)
            .await?,
        b"unicode case unchanged"
    );
    view.finalize().await?;
    let conn = sdk.get_connection().await?;
    let integrity = check(&conn, &CheckOpts::new(&db).check_base(true)).await?;
    assert!(integrity.ok, "{integrity:#?}");
    Ok(())
}
