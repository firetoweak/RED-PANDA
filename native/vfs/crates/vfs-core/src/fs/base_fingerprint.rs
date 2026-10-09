//! The same base metadata fingerprint for live handles and offline paths.
use super::Stats;
use crate::error::Result;
use std::path::Path;

#[derive(Debug, Clone, Copy, Eq, PartialEq, serde::Serialize)]
pub struct BaseFingerprint {
    pub size: i64,
    pub mtime: i64,
    pub mtime_nsec: i64,
    pub ctime: i64,
    pub ctime_nsec: i64,
}
impl BaseFingerprint {
    pub fn from_stats(stats: &Stats) -> Self {
        Self {
            size: stats.size,
            mtime: stats.mtime,
            mtime_nsec: stats.mtime_nsec as i64,
            ctime: stats.ctime,
            ctime_nsec: stats.ctime_nsec as i64,
        }
    }
    #[cfg(unix)]
    pub fn from_path(path: &Path) -> Result<Self> {
        use std::os::unix::fs::MetadataExt;
        let metadata = std::fs::metadata(path)?;
        Ok(Self {
            size: metadata.len() as i64,
            mtime: metadata.mtime(),
            mtime_nsec: metadata.mtime_nsec(),
            ctime: metadata.ctime(),
            ctime_nsec: metadata.ctime_nsec(),
        })
    }
    #[cfg(windows)]
    pub fn from_windows_info(
        basic: &windows_sys::Win32::Storage::FileSystem::FILE_BASIC_INFO,
        standard: &windows_sys::Win32::Storage::FileSystem::FILE_STANDARD_INFO,
    ) -> Self {
        let (mtime, mtime_nsec) = windows_timestamp(basic.LastWriteTime);
        let (ctime, ctime_nsec) = windows_timestamp(basic.ChangeTime);
        Self {
            size: standard.EndOfFile,
            mtime,
            mtime_nsec: mtime_nsec as i64,
            ctime,
            ctime_nsec: ctime_nsec as i64,
        }
    }
    #[cfg(windows)]
    pub fn from_path(path: &Path) -> Result<Self> {
        use std::os::windows::{fs::OpenOptionsExt, io::AsRawHandle};
        use windows_sys::Win32::Storage::FileSystem::*;
        let file = std::fs::OpenOptions::new()
            .access_mode(FILE_READ_ATTRIBUTES)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path)?;
        let mut basic = FILE_BASIC_INFO::default();
        let mut standard = FILE_STANDARD_INFO::default();
        // Each information class is paired with its documented C structure.
        for (class, pointer, size) in [
            (
                FileBasicInfo,
                (&mut basic as *mut FILE_BASIC_INFO).cast(),
                std::mem::size_of::<FILE_BASIC_INFO>(),
            ),
            (
                FileStandardInfo,
                (&mut standard as *mut FILE_STANDARD_INFO).cast(),
                std::mem::size_of::<FILE_STANDARD_INFO>(),
            ),
        ] {
            let ok = unsafe {
                GetFileInformationByHandleEx(file.as_raw_handle(), class, pointer, size as u32)
            };
            if ok == 0 {
                return Err(std::io::Error::last_os_error().into());
            }
        }
        Ok(Self::from_windows_info(&basic, &standard))
    }
}
#[cfg(windows)]
pub fn windows_timestamp(ticks: i64) -> (i64, u32) {
    let unix = ticks - 116_444_736_000_000_000;
    (
        unix.div_euclid(10_000_000),
        (unix.rem_euclid(10_000_000) * 100) as u32,
    )
}
