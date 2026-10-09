#![cfg(all(windows, feature = "winfsp"))]
use anyhow::Result;
use std::{os::windows::ffi::OsStrExt, sync::Arc};
use vfs_core::{HostFS, OverlayFS, Vfs, VfsOptions};
use vfs_mount::{mount_fs, Backend, MountOpts};

#[link(name = "kernel32")]
unsafe extern "system" {
    fn SetFileAttributesW(path: *const u16, attributes: u32) -> i32;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "requires installed WinFsp runtime and SDK build"]
async fn readonly_attribute_uses_core_mode_and_other_attributes_are_refused() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let base = temp.path().join("base");
    std::fs::create_dir(&base)?;
    std::fs::write(base.join("file"), b"host bytes")?;
    let sdk = Vfs::open(VfsOptions::with_path(
        temp.path().join("delta.db").to_string_lossy(),
    ))
    .await?;
    let fs = Arc::new(OverlayFS::new(
        Arc::new(HostFS::new(&base)?),
        sdk.fs.clone(),
    ));
    fs.init(base.to_str().unwrap()).await?;
    let point = temp.path().join("mount");
    let mount = mount_fs(fs, MountOpts::new(point.clone(), Backend::WinFsp)).await?;
    let path = point.join("file");
    let opened = std::fs::File::open(&path)?;
    let mut perms = std::fs::metadata(&path)?.permissions();
    perms.set_readonly(true);
    std::fs::set_permissions(&path, perms)?;
    assert!(std::fs::metadata(&path)?.permissions().readonly());
    assert!(opened.metadata()?.permissions().readonly());
    let wide: Vec<_> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    assert_ne!(unsafe { SetFileAttributesW(wide.as_ptr(), 128) }, 0);
    assert!(!std::fs::metadata(&path)?.permissions().readonly());
    assert!(!opened.metadata()?.permissions().readonly());
    assert_eq!(unsafe { SetFileAttributesW(wide.as_ptr(), 2) }, 0);
    assert_eq!(std::io::Error::last_os_error().raw_os_error(), Some(50));
    drop(opened);
    mount.unmount().await?;
    assert_eq!(std::fs::read(base.join("file"))?, b"host bytes");
    assert!(!std::fs::metadata(base.join("file"))?
        .permissions()
        .readonly());
    Ok(())
}
