#[cfg(target_os = "linux")]
#[path = "fuse.rs"]
mod fuse;
use anyhow::Result;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

/// Default timeout for mount to become ready.
const DEFAULT_MOUNT_TIMEOUT: Duration = Duration::from_secs(10);

#[cfg(target_os = "linux")]
fn get_runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Runtime::new().expect("internal error: failed to initialize runtime")
}

/// Mount backend type.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Default)]
pub enum Backend {
    /// FUSE filesystem (Linux only).
    #[default]
    Fuse,
}

impl std::fmt::Display for Backend {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Backend::Fuse => write!(f, "fuse"),
        }
    }
}

/// Options for mounting a filesystem.
///
/// Use `MountOpts::new()` to create default options, then customize as needed.
#[derive(Debug, Clone)]
pub struct MountOpts {
    /// The mountpoint path.
    pub mountpoint: PathBuf,
    /// Mount backend to use.
    pub backend: Backend,
    /// Filesystem name shown in mount output.
    pub fsname: String,
    /// User ID to report for all files.
    pub uid: Option<u32>,
    /// Group ID to report for all files.
    pub gid: Option<u32>,
    /// Allow other system users to access the mount.
    pub allow_other: bool,
    /// Allow root to access the mount (FUSE only).
    pub allow_root: bool,
    /// Auto unmount when process exits (FUSE only).
    pub auto_unmount: bool,
    /// Use lazy unmount on cleanup.
    pub lazy_unmount: bool,
    /// Timeout for mount to become ready.
    pub timeout: Duration,
}

impl MountOpts {
    /// Create default options for the given mountpoint and backend.
    pub fn new(mountpoint: PathBuf, backend: Backend) -> Self {
        Self {
            mountpoint,
            backend,
            fsname: "vfs".to_string(),
            uid: None,
            gid: None,
            allow_other: false,
            allow_root: false,
            auto_unmount: false,
            lazy_unmount: false,
            timeout: DEFAULT_MOUNT_TIMEOUT,
        }
    }
}

impl Default for MountOpts {
    fn default() -> Self {
        Self::new(PathBuf::new(), Backend::default())
    }
}

/// A mounted filesystem handle.
///
/// This handle represents an active mount. Prefer calling [`MountHandle::unmount`]
/// so the backend can join all worker tasks and surface teardown errors. Drop is
/// retained as best-effort cleanup for early-return paths.
pub struct MountHandle {
    mountpoint: PathBuf,
    backend: Backend,
    lazy_unmount: bool,
    inner: MountHandleInner,
}

pub(crate) enum MountHandleInner {
    #[cfg(target_os = "linux")]
    Fuse {
        session: Option<vfs_fuse::SessionHandle>,
    },
}

impl MountHandle {
    /// Get the mountpoint path.
    pub fn mountpoint(&self) -> &Path {
        &self.mountpoint
    }

    /// Unmount and join all backend-owned work.
    ///
    /// FUSE teardown requests the session unmount, joins the session thread
    /// (which drains FUSE dispatch workers and uring queue threads), then
    /// verifies the mountpoint is no longer mounted.
    pub async fn unmount(mut self) -> Result<()> {
        self.unmount_inner_async().await
    }

    async fn unmount_inner_async(&mut self) -> Result<()> {
        let _ = std::env::set_current_dir("/");
        let mut first_error = None;

        match &mut self.inner {
            #[cfg(target_os = "linux")]
            MountHandleInner::Fuse { session } => {
                if let Some(session) = session.as_mut() {
                    if let Err(error) = session.unmount() {
                        remember_error(
                            &mut first_error,
                            anyhow::anyhow!(
                                "failed to request FUSE session unmount at {}: {}",
                                self.mountpoint.display(),
                                error
                            ),
                        );
                    }
                }
                if is_mountpoint(&self.mountpoint) {
                    if let Err(error) = unmount(&self.mountpoint, self.backend, self.lazy_unmount) {
                        remember_error(&mut first_error, error);
                    }
                }
                if let Some(session) = session.take() {
                    if let Err(error) = session.join() {
                        remember_error(&mut first_error, error);
                    }
                }
                if is_mountpoint(&self.mountpoint) {
                    if let Err(error) = unmount(&self.mountpoint, self.backend, self.lazy_unmount) {
                        remember_error(&mut first_error, error);
                    }
                }
                if is_mountpoint(&self.mountpoint) {
                    remember_error(
                        &mut first_error,
                        anyhow::anyhow!(
                            "FUSE mountpoint {} is still mounted after teardown",
                            self.mountpoint.display()
                        ),
                    );
                }
            }
        }

        match first_error {
            Some(error) => Err(error),
            None => Ok(()),
        }
    }

    fn unmount_inner_sync(&mut self) {
        let _ = std::env::set_current_dir("/");

        match &mut self.inner {
            #[cfg(target_os = "linux")]
            MountHandleInner::Fuse { session } => {
                if let Some(session) = session.as_mut() {
                    if let Err(error) = session.unmount() {
                        tracing::warn!(
                            mountpoint = %self.mountpoint.display(),
                            %error,
                            "failed to request FUSE session unmount"
                        );
                    }
                }
                if is_mountpoint(&self.mountpoint) {
                    if let Err(error) = unmount(&self.mountpoint, self.backend, self.lazy_unmount) {
                        tracing::warn!(
                            mountpoint = %self.mountpoint.display(),
                            %error,
                            "failed to unmount FUSE filesystem"
                        );
                    }
                }
                if let Some(session) = session.take() {
                    if let Err(error) = session.join() {
                        tracing::warn!(%error, "FUSE session exited with error");
                    }
                }
                if is_mountpoint(&self.mountpoint) {
                    if let Err(error) = unmount(&self.mountpoint, self.backend, self.lazy_unmount) {
                        tracing::warn!(
                            mountpoint = %self.mountpoint.display(),
                            %error,
                            "failed final FUSE unmount"
                        );
                    }
                }
                if is_mountpoint(&self.mountpoint) {
                    tracing::warn!(
                        mountpoint = %self.mountpoint.display(),
                        "FUSE mountpoint is still mounted after teardown"
                    );
                }
            }
        }
    }
}

impl Drop for MountHandle {
    fn drop(&mut self) {
        self.unmount_inner_sync();
    }
}

/// Unmount a filesystem at the given mountpoint.
///
/// If `lazy` is true, uses lazy unmount which detaches immediately even if busy.
pub fn unmount(mountpoint: &Path, backend: Backend, lazy: bool) -> Result<()> {
    match backend {
        #[cfg(target_os = "linux")]
        Backend::Fuse => fuse::unmount_fuse(mountpoint, lazy),
        #[cfg(not(target_os = "linux"))]
        Backend::Fuse => anyhow::bail!("FUSE is not supported on this platform"),
    }
}

/// Mount a filesystem with the given options.
///
/// Returns a handle that automatically unmounts when dropped.
/// The filesystem must be wrapped in `Arc<dyn FileSystem>`.
#[cfg(target_os = "linux")]
pub async fn mount_fs(fs: Arc<dyn vfs_core::FileSystem>, opts: MountOpts) -> Result<MountHandle> {
    match opts.backend {
        Backend::Fuse => fuse::mount_fuse(fs, opts),
    }
}

fn remember_error(slot: &mut Option<anyhow::Error>, error: anyhow::Error) {
    if slot.is_none() {
        *slot = Some(error);
    }
}

/// Wait for a path to become a mountpoint.
///
#[cfg(target_os = "linux")]
pub(crate) fn wait_for_mount(path: &Path, timeout: Duration) -> bool {
    let start = std::time::Instant::now();
    let interval = Duration::from_millis(50);

    while start.elapsed() < timeout {
        if is_mountpoint(path) {
            return true;
        }
        std::thread::sleep(interval);
    }
    false
}

/// Check if a path is present in the process mount table.
///
/// Linux intentionally parses `/proc/self/mountinfo` instead of statting the
/// path: metadata access to a dead FUSE connection can block or return
/// `ENOTCONN`, while the mount table remains authoritative and non-blocking.
pub fn is_mountpoint(path: &Path) -> bool {
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::ffi::OsStrExt;

        let absolute = match std::path::absolute(path) {
            Ok(path) => path,
            Err(_) => return false,
        };
        let mountinfo = match std::fs::read("/proc/self/mountinfo") {
            Ok(mountinfo) => mountinfo,
            Err(_) => return false,
        };

        mountinfo.split(|byte| *byte == b'\n').any(|line| {
            let Some(field) = line.split(|byte| *byte == b' ').nth(4) else {
                return false;
            };
            unescape_mountinfo_field(field) == absolute.as_os_str().as_bytes()
        })
    }

    #[cfg(all(unix, not(target_os = "linux")))]
    {
        use std::os::unix::fs::MetadataExt;

        let path_meta = match std::fs::metadata(path) {
            Ok(m) => m,
            Err(_) => return false,
        };

        let parent = match path.parent() {
            Some(p) if !p.as_os_str().is_empty() => p,
            _ => Path::new("/"),
        };

        let parent_meta = match std::fs::metadata(parent) {
            Ok(m) => m,
            Err(_) => return false,
        };

        path_meta.dev() != parent_meta.dev()
    }

    #[cfg(not(unix))]
    {
        let _ = path;
        false
    }
}

#[cfg(target_os = "linux")]
fn unescape_mountinfo_field(field: &[u8]) -> Vec<u8> {
    let mut output = Vec::with_capacity(field.len());
    let mut index = 0;
    while index < field.len() {
        if field[index] == b'\\'
            && index + 3 < field.len()
            && field[index + 1..index + 4]
                .iter()
                .all(|byte| matches!(byte, b'0'..=b'7'))
        {
            let value = (field[index + 1] - b'0') * 64
                + (field[index + 2] - b'0') * 8
                + (field[index + 3] - b'0');
            output.push(value);
            index += 4;
        } else {
            output.push(field[index]);
            index += 1;
        }
    }
    output
}

#[cfg(all(test, target_os = "linux"))]
mod mountinfo_tests {
    use super::unescape_mountinfo_field;

    #[test]
    fn unescapes_mountinfo_paths_without_touching_the_mount() {
        assert_eq!(
            unescape_mountinfo_field(br"/tmp/a\040b\011c\134d"),
            b"/tmp/a b\tc\\d"
        );
    }
}
