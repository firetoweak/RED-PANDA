//! WinFsp lifecycle. The C bridge owns SDK structures; this adapter owns handles.
use anyhow::{anyhow, Result};
use std::{
    collections::BTreeMap,
    ffi::c_void,
    os::windows::ffi::OsStrExt,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};
use vfs_core::{error::Error, BoxedFile, FileSystem, FsError, Stats};
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum Backend {
    #[default]
    WinFsp,
}
#[derive(Debug, Clone)]
pub struct MountOpts {
    pub mountpoint: PathBuf,
    pub backend: Backend,
}
impl MountOpts {
    pub fn new(mountpoint: PathBuf, backend: Backend) -> Self {
        Self {
            mountpoint,
            backend,
        }
    }
}
struct Opened {
    ino: i64,
    file: Option<BoxedFile>,
}
struct State {
    fs: Arc<dyn FileSystem>,
    handles: BTreeMap<u64, Opened>,
    next: u64,
    lookups: BTreeMap<i64, u64>,
    fatal: Option<anyhow::Error>,
}
struct CallbackContext {
    runtime: tokio::runtime::Handle,
    state: Mutex<State>,
}
type Callback = unsafe extern "C" fn(
    *mut c_void,
    u32,
    u64,
    u64,
    u32,
    *const c_void,
    *mut *mut c_void,
    *mut u32,
) -> i32;
unsafe extern "C" {
    fn vfs_bridge_start(
        path: *const u16,
        callback: Callback,
        context: *mut c_void,
        case_sensitive: i32,
        fs: *mut *mut c_void,
    ) -> i32;
    fn vfs_bridge_stop(fs: *mut c_void);
    fn vfs_bridge_alloc(length: usize) -> *mut c_void;
}
pub struct MountHandle {
    mountpoint: PathBuf,
    native: usize,
    context: Option<Box<CallbackContext>>,
}
impl MountHandle {
    pub fn mountpoint(&self) -> &Path {
        &self.mountpoint
    }
    /// Stop callbacks before releasing their Rust context, then commit the filesystem.
    pub async fn unmount(mut self) -> Result<()> {
        let (native, context) = (self.native, self.context.take().unwrap());
        self.native = 0;
        tokio::task::spawn_blocking(move || {
            // SAFETY: SDK dispatcher is still owned; stop joins callbacks before context drops.
            unsafe {
                vfs_bridge_stop(native as *mut c_void);
            }
            let mut state = context
                .state
                .lock()
                .unwrap_or_else(|poison| poison.into_inner());
            let finish = context.runtime.block_on(state.finish());
            match state.fatal.take() {
                Some(error) => Err(error),
                None => finish,
            }
        })
        .await?
    }
}
impl Drop for MountHandle {
    fn drop(&mut self) {
        if self.native == 0 {
            return;
        }
        // Best effort on early return; durable success is only claimed by explicit unmount.
        unsafe {
            vfs_bridge_stop(self.native as *mut c_void);
        }
        self.native = 0;
        if let Some(context) = self.context.take() {
            let runtime = context.runtime.clone();
            runtime.spawn(async move {
                let mut state = context
                    .state
                    .into_inner()
                    .unwrap_or_else(|poison| poison.into_inner());
                if let Err(error) = state.finish().await {
                    tracing::error!(%error,"WinFsp drop finalization failed");
                }
                if let Some(error) = state.fatal {
                    tracing::error!(%error,"WinFsp callback failed");
                }
            });
        }
    }
}
pub async fn mount_fs(fs: Arc<dyn FileSystem>, opts: MountOpts) -> Result<MountHandle> {
    let path: Vec<u16> = opts
        .mountpoint
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect();
    if path[..path.len() - 1].contains(&0) {
        return Err(anyhow!("mountpoint contains NUL"));
    }
    let case_sensitive = !fs.names_equal("a", "A");
    let mut context = Box::new(CallbackContext {
        runtime: tokio::runtime::Handle::current(),
        state: Mutex::new(State {
            fs,
            handles: BTreeMap::new(),
            next: 2,
            lookups: BTreeMap::new(),
            fatal: None,
        }),
    });
    let mut native = std::ptr::null_mut();
    // SDK uses a dedicated dispatcher thread; callbacks never run on the caller's runtime worker.
    let status = unsafe {
        vfs_bridge_start(
            path.as_ptr(),
            callback,
            (&mut *context as *mut CallbackContext).cast(),
            case_sensitive as i32,
            &mut native,
        )
    };
    if status < 0 {
        return Err(anyhow!(
            "WinFsp mount failed: NTSTATUS {:08x}",
            status as u32
        ));
    }
    Ok(MountHandle {
        mountpoint: opts.mountpoint,
        native: native as usize,
        context: Some(context),
    })
}
fn pack(stats: &Stats) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(28);
    bytes.extend_from_slice(&(stats.ino as u64).to_le_bytes());
    bytes.extend_from_slice(&(stats.size as u64).to_le_bytes());
    let ticks = ((stats.mtime as i128 + 11_644_473_600) * 10_000_000
        + stats.mtime_nsec as i128 / 100) as u64;
    bytes.extend_from_slice(&ticks.to_le_bytes());
    bytes.extend_from_slice(
        &((if stats.is_directory() {
            0x10u32
        } else if stats.mode & 0o222 == 0 {
            0
        } else {
            0x80u32
        }) | if stats.mode & 0o222 == 0 { 1 } else { 0 })
        .to_le_bytes(),
    );
    bytes
}
fn name(body: &[u8]) -> vfs_core::error::Result<&str> {
    let path = std::str::from_utf8(body).map_err(|_| FsError::InvalidPath)?;
    if path.is_empty()
        || path
            .split('/')
            .any(|c| c.is_empty() || c == "." || c == ".." || c.contains(['\\', ':', '\0']))
    {
        return Err(FsError::InvalidPath.into());
    }
    Ok(path)
}
impl State {
    async fn resolve(&mut self, path: &str) -> vfs_core::error::Result<Stats> {
        self.resolve_named(path).await.map(|(stats, _)| stats)
    }
    async fn resolve_named(&mut self, path: &str) -> vfs_core::error::Result<(Stats, String)> {
        let mut stats = self.fs.getattr(1).await?.ok_or(FsError::NotFound)?;
        let mut normalized = String::new();
        for component in path.split('/') {
            let entry = self
                .fs
                .lookup_named(stats.ino, component)
                .await?
                .ok_or(FsError::NotFound)?;
            stats = entry.stats;
            *self.lookups.entry(stats.ino).or_default() += 1;
            if !normalized.is_empty() {
                normalized.push('/');
            }
            normalized.push_str(&entry.name);
        }
        Ok((stats, normalized))
    }
    async fn parent(&mut self, path: &str) -> vfs_core::error::Result<(i64, String)> {
        match path.rsplit_once('/') {
            Some((parent, child)) => Ok((self.resolve(parent).await?.ino, child.to_owned())),
            None => Ok((1, path.to_owned())),
        }
    }
    async fn info(&self, id: u64) -> vfs_core::error::Result<Stats> {
        if id == 1 {
            return self.fs.getattr(1).await?.ok_or(FsError::NotFound.into());
        }
        let opened = self
            .handles
            .get(&id)
            .expect("WinFsp handle exists until cleanup");
        match &opened.file {
            Some(file) => {
                let mut stats = file.fstat().await?;
                if let Some(logical) = self.fs.getattr(opened.ino).await? {
                    stats.mode = logical.mode;
                }
                Ok(stats)
            }
            None => self
                .fs
                .getattr(opened.ino)
                .await?
                .ok_or(FsError::NotFound.into()),
        }
    }
    fn file(&self, id: u64) -> vfs_core::error::Result<BoxedFile> {
        self.handles
            .get(&id)
            .expect("WinFsp file handle exists")
            .file
            .clone()
            .ok_or(FsError::IsADirectory.into())
    }
    async fn dispatch(
        &mut self,
        op: u32,
        id: u64,
        offset: u64,
        length: u32,
        body: &[u8],
    ) -> vfs_core::error::Result<Vec<u8>> {
        match op {
            1 => self.file(id)?.pread(offset, length as u64).await,
            2 => {
                self.file(id)?.pwrite(offset, body).await?;
                Ok(pack(&self.info(id).await?))
            }
            3 => Ok(pack(&self.info(id).await?)),
            4 | 7 => {
                let path = name(body)?;
                let (stats, file, normalized) = if op == 7 {
                    let (parent, child) = self.parent(path).await?;
                    let (stats, file) = if offset != 0 {
                        (self.fs.mkdir(parent, &child, 0o755, 0, 0).await?, None)
                    } else {
                        let (stats, file) =
                            self.fs.create_file(parent, &child, 0o644, 0, 0).await?;
                        (stats, Some(file))
                    };
                    *self.lookups.entry(stats.ino).or_default() += 1;
                    let (_, normalized) = self.resolve_named(path).await?;
                    (stats, file, normalized)
                } else {
                    let (stats, normalized) = self.resolve_named(path).await?;
                    let file = if stats.is_directory() {
                        None
                    } else {
                        Some(
                            self.fs
                                .open(
                                    stats.ino,
                                    if offset != 0 {
                                        libc::O_RDWR
                                    } else {
                                        libc::O_RDONLY
                                    },
                                )
                                .await?,
                        )
                    };
                    let stats = match &file {
                        Some(file) => file.fstat().await?,
                        None => stats,
                    };
                    (stats, file, normalized)
                };
                let handle = self.next;
                self.next += 1;
                self.handles.insert(
                    handle,
                    Opened {
                        ino: stats.ino,
                        file,
                    },
                );
                let mut data = pack(&stats);
                data.extend_from_slice(&handle.to_le_bytes());
                data.extend_from_slice(normalized.as_bytes());
                Ok(data)
            }
            5 => {
                if let Some(file) = &self.handles.get(&id).expect("cleanup of open handle").file {
                    file.fsync().await?;
                }
                if offset != 0 {
                    let path = name(body)?;
                    let (parent, child) = self.parent(path).await?;
                    let opened = self.info(id).await?;
                    let current = self
                        .fs
                        .lookup(parent, &child)
                        .await?
                        .ok_or(FsError::NotFound)?;
                    *self.lookups.entry(current.ino).or_default() += 1;
                    if current.ino != opened.ino {
                        return Err(FsError::Corrupt(
                            "delete target differs from open object".into(),
                        )
                        .into());
                    }
                    if opened.is_directory() {
                        self.fs.rmdir(parent, &child).await?;
                    } else {
                        self.fs.unlink(parent, &child).await?;
                    }
                }
                Ok(Vec::new())
            }
            6 => Ok(pack(&self.resolve(name(body)?).await?)),
            8 => {
                let separator = body
                    .iter()
                    .position(|c| *c == 0)
                    .ok_or(FsError::InvalidPath)?;
                let from = name(&body[..separator])?;
                let to = name(&body[separator + 1..])?;
                let (oldparent, oldname) = self.parent(from).await?;
                let (parent, child) = self.parent(to).await?;
                if let Some(target) = self.fs.lookup(parent, &child).await? {
                    *self.lookups.entry(target.ino).or_default() += 1;
                    let same_name = oldparent == parent && self.fs.names_equal(&oldname, &child);
                    if offset == 0 && !same_name {
                        return Err(FsError::AlreadyExists.into());
                    }
                    if target.is_directory() {
                        return Err(FsError::IsADirectory.into());
                    }
                }
                self.fs.rename(oldparent, &oldname, parent, &child).await?;
                Ok(Vec::new())
            }
            9 => {
                self.file(id)?.truncate(offset).await?;
                Ok(pack(&self.info(id).await?))
            }
            11 => {
                if offset != 0 {
                    let stats = self.info(id).await?;
                    if stats.is_directory()
                        && !self
                            .fs
                            .readdir(stats.ino)
                            .await?
                            .ok_or(FsError::NotFound)?
                            .is_empty()
                    {
                        return Err(FsError::NotEmpty.into());
                    }
                }
                Ok(Vec::new())
            }
            12 => {
                let stats = self.info(id).await?;
                let mut result = Vec::new();
                for entry in self
                    .fs
                    .readdir_plus(stats.ino)
                    .await?
                    .ok_or(FsError::NotFound)?
                {
                    *self.lookups.entry(entry.stats.ino).or_default() += 1;
                    result.extend_from_slice(&(entry.name.len() as u32).to_le_bytes());
                    result.extend(pack(&entry.stats));
                    result.extend(entry.name.as_bytes());
                }
                Ok(result)
            }
            13 => {
                if id > 1 {
                    if let Some(file) = &self.handles.get(&id).expect("flush of open handle").file {
                        file.fsync().await?;
                    }
                } else {
                    for handle in self.handles.values() {
                        if let Some(file) = &handle.file {
                            file.fsync().await?;
                        }
                    }
                    self.fs.drain_all().await?;
                }
                Ok(Vec::new())
            }
            15 => {
                self.handles.remove(&id).expect("close of open handle");
                Ok(Vec::new())
            }
            16 => {
                if offset & !(1 | 0x10 | 0x80) != 0 {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::Unsupported,
                        "only the read-only attribute has a core representation",
                    )
                    .into());
                }
                let ino = if id == 1 {
                    1
                } else {
                    self.handles.get(&id).expect("basic info handle exists").ino
                };
                let stats = self.fs.getattr(ino).await?.ok_or(FsError::NotFound)?;
                let mode = if offset & 1 != 0 {
                    stats.mode & !0o222
                } else {
                    stats.mode | 0o222
                };
                if mode != stats.mode {
                    self.fs.chmod(ino, mode).await?;
                }
                Ok(pack(&self.fs.getattr(ino).await?.ok_or(FsError::NotFound)?))
            }
            _ => Err(Error::Internal(format!("unknown WinFsp callback {op}"))),
        }
    }
    async fn finish(&mut self) -> Result<()> {
        let mut first = None;
        for handle in self.handles.values() {
            if let Some(file) = &handle.file {
                if let Err(error) = file.fsync().await {
                    if first.is_none() {
                        first = Some(error);
                    }
                }
            }
        }
        self.handles.clear();
        for (ino, count) in std::mem::take(&mut self.lookups) {
            self.fs.forget(ino, count).await;
        }
        let final_result = self.fs.finalize().await;
        match first {
            Some(error) => Err(error.into()),
            None => final_result.map_err(Into::into),
        }
    }
}
fn status(error: &Error) -> Option<i32> {
    let value: u32 = match error {
        Error::Fs(e) => match e {
            FsError::NotFound => 0xc0000034,
            FsError::AlreadyExists => 0xc0000035,
            FsError::NotEmpty => 0xc0000101,
            FsError::NotADirectory => 0xc0000103,
            FsError::IsADirectory => 0xc00000ba,
            FsError::PermissionDenied | FsError::OperationNotPermitted | FsError::RootOperation => {
                0xc0000022
            }
            FsError::CrossDevice => 0xc00000d4,
            FsError::InvalidPath
            | FsError::InvalidRename
            | FsError::BadCookie
            | FsError::NotASymlink => 0xc000000d,
            FsError::NameTooLong => 0xc0000106,
            FsError::SymlinkLoop => 0xc0000280,
            FsError::Corrupt(_) => return None,
        },
        Error::Io(e) => match e.kind() {
            std::io::ErrorKind::NotFound => 0xc0000034,
            std::io::ErrorKind::PermissionDenied => 0xc0000022,
            std::io::ErrorKind::Unsupported => 0xc00000bb,
            std::io::ErrorKind::StorageFull => 0xc000007f,
            _ => return None,
        },
        _ => return None,
    };
    Some(value as i32)
}
unsafe extern "C" fn callback(
    context: *mut c_void,
    op: u32,
    id: u64,
    offset: u64,
    length: u32,
    input: *const c_void,
    output: *mut *mut c_void,
    outlen: *mut u32,
) -> i32 {
    // SAFETY: C owns input for this call; output uses the bridge's allocator.
    let context = &*(context as *const CallbackContext);
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let mut state = context
            .state
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        if state.fatal.is_some() {
            return 0xc00000e5u32 as i32;
        }
        let body = if input.is_null() {
            &[]
        } else {
            std::slice::from_raw_parts(input as *const u8, length as usize)
        };
        match context
            .runtime
            .block_on(state.dispatch(op, id, offset, length, body))
        {
            Ok(data) => {
                let pointer = vfs_bridge_alloc(data.len());
                if pointer.is_null() {
                    return 0xc000009au32 as i32;
                }
                std::ptr::copy_nonoverlapping(data.as_ptr(), pointer as *mut u8, data.len());
                *output = pointer;
                *outlen = data.len().try_into().expect("WinFsp output fits u32");
                0
            }
            Err(error) => {
                if matches!(op, 5 | 15) || status(&error).is_none() {
                    tracing::error!(?error, op, "WinFsp callback failed");
                    state.fatal = Some(error.into());
                    0xc00000e5u32 as i32
                } else {
                    status(&error).unwrap()
                }
            }
        }
    }));
    match outcome {
        Ok(status) => status,
        Err(panic) => {
            let message = panic
                .downcast_ref::<String>()
                .cloned()
                .or_else(|| panic.downcast_ref::<&str>().map(|s| s.to_string()))
                .unwrap_or_else(|| "non-string panic".into());
            let mut state = context
                .state
                .lock()
                .unwrap_or_else(|poison| poison.into_inner());
            if state.fatal.is_none() {
                state.fatal = Some(anyhow!("WinFsp callback panicked: {message}"));
            }
            0xc00000e5u32 as i32
        }
    }
}
