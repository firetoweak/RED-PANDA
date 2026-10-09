//! Platform mount lifecycle; filesystem semantics belong to vfs-core.
#[cfg(unix)]
mod unix;
#[cfg(unix)]
pub use unix::*;
#[cfg(all(windows, feature = "winfsp"))]
mod windows;
#[cfg(all(windows, feature = "winfsp"))]
pub use windows::{mount_fs, Backend, MountHandle, MountOpts};
