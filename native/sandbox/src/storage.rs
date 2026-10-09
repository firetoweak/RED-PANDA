use anyhow::{ensure, Context, Result};
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use sha2::{Digest, Sha256};
#[cfg(unix)]
use std::os::unix::{fs::PermissionsExt, io::AsRawFd};
#[cfg(windows)]
use std::os::windows::{ffi::OsStrExt, fs::OpenOptionsExt};
use std::{
    fs::{File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::Arc,
};
use vfs_core::{FileSystem, HostFS, OverlayFS, PartialOriginMode, PartialOriginPolicy, Vfs};

pub fn policy() -> PartialOriginPolicy {
    PartialOriginPolicy::new(PartialOriginMode::On)
}
pub fn hash_reader(mut reader: impl Read) -> Result<(String, u64)> {
    let mut h = Sha256::new();
    let mut length = 0;
    let mut buffer = [0; 65536];
    loop {
        let n = reader.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        h.update(&buffer[..n]);
        length += n as u64;
    }
    Ok((format!("{:x}", h.finalize()), length))
}
pub fn read_json<T: DeserializeOwned>(path: &Path) -> Result<T> {
    Ok(serde_json::from_slice(&std::fs::read(path)?)?)
}
pub fn durable(path: &Path, bytes: &[u8]) -> Result<()> {
    let mut file = OpenOptions::new().write(true).create_new(true).open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    Ok(())
}
#[cfg(windows)]
#[link(name = "kernel32")]
unsafe extern "system" {
    fn MoveFileExW(a: *const u16, b: *const u16, flags: u32) -> i32;
}
#[cfg(windows)]
pub fn replace(from: &Path, to: &Path) -> Result<()> {
    let a: Vec<_> = from.as_os_str().encode_wide().chain(Some(0)).collect();
    let b: Vec<_> = to.as_os_str().encode_wide().chain(Some(0)).collect();
    let attempt = || {
        if unsafe { MoveFileExW(a.as_ptr(), b.as_ptr(), 1 | 8) } == 0 {
            Err(std::io::Error::last_os_error())
        } else {
            Ok(())
        }
    };
    let mut result = attempt();
    // Only the unpublished metadata rename is retried; file effects are not replayed.
    for milliseconds in [10, 20, 40, 80] {
        if !matches!(
            result.as_ref().err().and_then(|e| e.raw_os_error()),
            Some(5 | 32)
        ) {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(milliseconds));
        result = attempt();
    }
    result.with_context(|| format!("replace internal metadata {}", to.display()))
}
#[cfg(unix)]
pub fn replace(from: &Path, to: &Path) -> Result<()> {
    std::fs::rename(from, to).with_context(|| format!("replace internal metadata {}", to.display()))
}
pub struct Pin {
    file: File,
    #[cfg(unix)]
    path: PathBuf,
    #[cfg(unix)]
    mode: u32,
}
impl std::io::Read for &Pin {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        (&self.file).read(buf)
    }
}
impl Drop for Pin {
    fn drop(&mut self) {
        #[cfg(unix)]
        {
            let _ =
                std::fs::set_permissions(&self.path, std::fs::Permissions::from_mode(self.mode));
        }
    }
}
fn open_shared_read(path: &Path) -> std::io::Result<Pin> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(windows)]
    options.share_mode(1);
    let file = options.open(path)?;
    #[cfg(unix)]
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_SH | libc::LOCK_NB) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    // Mode bits are the Linux stand-in for Windows exclusive sharing: a live
    // pin clears write bits so a same-user read-write open fails, then restores
    // the captured mode. Rename in a writable directory is unaffected.
    #[cfg(unix)]
    let mode = {
        let mode = std::fs::metadata(path)?.permissions().mode();
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode & !0o222))?;
        mode
    };
    Ok(Pin {
        file,
        #[cfg(unix)]
        path: path.to_owned(),
        #[cfg(unix)]
        mode,
    })
}
pub fn atomic_json(path: &Path, value: &impl Serialize) -> Result<()> {
    let temp = path.with_extension(format!("{}.next", uuid::Uuid::new_v4()));
    durable(&temp, &serde_json::to_vec(value)?)?;
    replace(&temp, path)
}
pub fn identifier(value: &str) -> Result<()> {
    ensure!(
        !value.is_empty()
            && value.len() <= 100
            && value
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_'),
        "invalid command id"
    );
    Ok(())
}
pub fn digest(value: &str) -> Result<()> {
    ensure!(
        value.len() == 64
            && value
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "invalid digest"
    );
    Ok(())
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Image {
    pub kind: String,
    pub size: u64,
    pub identity: Option<String>,
    pub chunk_size: u64,
    pub complete: bool,
    pub blocks: std::collections::BTreeMap<u64, Option<String>>,
    pub mode: Option<u32>,
    pub target: Option<String>,
}
impl Image {
    pub fn missing() -> Self {
        Self {
            kind: "missing".into(),
            size: 0,
            identity: None,
            chunk_size: vfs_core::config::DEFAULT_CHUNK_SIZE as u64,
            complete: true,
            blocks: Default::default(),
            mode: None,
            target: None,
        }
    }
}
pub async fn resolve(fs: &dyn FileSystem, path: &str) -> vfs_core::error::Result<Option<i64>> {
    let mut ino = 1;
    for name in path.split('/').filter(|s| !s.is_empty()) {
        let Some(s) = fs.lookup(ino, name).await? else {
            return Ok(None);
        };
        ino = s.ino;
    }
    Ok(Some(ino))
}
fn file_mode(mode: u32) -> Option<u32> {
    // Windows publication does not chmod. Recording a mode there would make
    // every host file look changed.
    #[cfg(unix)]
    {
        Some(mode & 0o777)
    }
    #[cfg(not(unix))]
    {
        let _ = mode;
        None
    }
}
pub async fn metadata(
    fs: &dyn FileSystem,
    path: &str,
    chunk_size: u64,
) -> vfs_core::error::Result<Image> {
    let Some(ino) = resolve(fs, path).await? else {
        return Ok(Image::missing());
    };
    let stats = fs.getattr(ino).await?.expect("resolved inode absent");
    let identity = Some(fs.file_identity(ino)?);
    if stats.is_directory() {
        let mut image = Image::missing();
        image.kind = "directory".into();
        image.identity = identity;
        image.chunk_size = chunk_size;
        return Ok(image);
    }
    if stats.is_symlink() {
        #[cfg(unix)]
        {
            let target = fs.readlink(ino).await?.ok_or_else(|| {
                std::io::Error::new(std::io::ErrorKind::NotFound, "symlink target missing")
            })?;
            let mut image = Image::missing();
            image.kind = "symlink".into();
            image.size = target.len() as u64;
            image.identity = identity;
            image.chunk_size = chunk_size;
            image.mode = Some(stats.mode & 0o777);
            image.target = Some(target);
            return Ok(image);
        }
        #[cfg(not(unix))]
        {
            let _ = identity;
            return Err(std::io::Error::new(
                std::io::ErrorKind::Unsupported,
                "only regular files and directories are supported",
            )
            .into());
        }
    }
    if !stats.is_file() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "only regular files, directories, and symlinks are supported",
        )
        .into());
    }
    Ok(Image {
        kind: "file".into(),
        size: stats.size as u64,
        identity,
        chunk_size,
        complete: false,
        blocks: Default::default(),
        mode: file_mode(stats.mode),
        target: None,
    })
}
pub fn save_block(cas: &Path, bytes: &[u8]) -> vfs_core::error::Result<String> {
    let value = format!("{:x}", Sha256::digest(bytes));
    let dest = cas.join(&value);
    if dest.exists() {
        let stored = File::open(&dest)?;
        if stored.metadata()?.len() != bytes.len() as u64 {
            return Err(std::io::Error::other("CAS evidence is corrupt").into());
        }
        let (actual, length) = hash_reader(stored)
            .map_err(|e| vfs_core::error::Error::Internal(format!("verify CAS: {e:#}")))?;
        if actual != value || length != bytes.len() as u64 {
            return Err(std::io::Error::other("CAS evidence is corrupt").into());
        }
    } else {
        let tmp = cas.join(format!("{}.tmp", uuid::Uuid::new_v4()));
        durable(&tmp, bytes)
            .map_err(|e| vfs_core::error::Error::Internal(format!("persist CAS: {e:#}")))?;
        std::fs::rename(tmp, dest)?;
    }
    Ok(value)
}
pub async fn capture_blocks(
    fs: &dyn FileSystem,
    path: &str,
    cas: &Path,
    image: &mut Image,
    mut indexes: impl Iterator<Item = u64>,
) -> vfs_core::error::Result<()> {
    if image.kind != "file" {
        return Ok(());
    }
    let Some(first) = indexes.find(|index| !image.blocks.contains_key(index)) else {
        return Ok(());
    };
    let ino = resolve(fs, path).await?.expect("capture inode absent");
    let file = fs.open(ino, libc::O_RDONLY).await?;
    let initial = file.fstat().await?;
    for index in std::iter::once(first).chain(indexes) {
        let offset = index
            .checked_mul(image.chunk_size)
            .expect("block offset overflow");
        if image.blocks.contains_key(&index) {
            continue;
        }
        if offset >= image.size {
            image.blocks.insert(index, None);
            continue;
        }
        let length = (image.size - offset).min(image.chunk_size);
        let bytes = file.pread(offset, length).await?;
        if bytes.len() as u64 != length {
            return Err(
                std::io::Error::new(std::io::ErrorKind::UnexpectedEof, "image shortened").into(),
            );
        }
        image.blocks.insert(index, Some(save_block(cas, &bytes)?));
    }
    let end = file.fstat().await?;
    if end.size != initial.size
        || end.mtime != initial.mtime
        || end.mtime_nsec != initial.mtime_nsec
        || end.ctime != initial.ctime
        || end.ctime_nsec != initial.ctime_nsec
    {
        return Err(std::io::Error::other("file changed during evidence capture").into());
    }
    Ok(())
}
pub async fn after_image(
    fs: &dyn FileSystem,
    path: &str,
    cas: &Path,
    before: &Image,
) -> vfs_core::error::Result<Image> {
    let mut value = metadata(fs, path, before.chunk_size).await?;
    if before.complete || before.kind != value.kind {
        let count = value.size.div_ceil(value.chunk_size);
        capture_blocks(fs, path, cas, &mut value, 0..count).await?;
        value.complete = true;
    } else {
        capture_blocks(fs, path, cas, &mut value, before.blocks.keys().copied()).await?;
    }
    Ok(value)
}
pub struct Parent {
    pub fs: Arc<dyn FileSystem>,
    pub host: Arc<HostFS>,
    pub pins: Vec<Pin>,
    pub artifacts: Vec<String>,
    pub depth: usize,
    pub hashed_bytes: u64,
}
impl Parent {
    pub async fn cold(store: &Path, base: &Path, tip: Option<&str>) -> Result<Self> {
        let host = Arc::new(HostFS::new(base)?);
        let mut parent = Self {
            fs: host.clone(),
            host,
            pins: vec![],
            artifacts: vec![],
            depth: 0,
            hashed_bytes: 0,
        };
        let mut chain = vec![];
        let mut next = tip.map(str::to_owned);
        let mut seen = std::collections::HashSet::new();
        while let Some(h) = next {
            ensure!(seen.insert(h.clone()), "cyclic parent chain");
            digest(&h)?;
            let path = store.join("artifacts").join(format!("{h}.db"));
            // Keep the exact native file immutable while its validated SDK view is reusable.
            let pin = open_shared_read(&path)?;
            let (actual, length) = hash_reader(&pin)?;
            ensure!(actual == h, "artifact digest mismatch");
            parent.hashed_bytes += length;
            let sdk = Vfs::open_read_only(&path).await?;
            ensure!(
                sdk.overlay_base_path().await?.as_deref() == base.to_str(),
                "base path changed"
            );
            next = sdk.overlay_parent_artifact().await?;
            chain.push((sdk, pin, h));
        }
        for (sdk, pin, h) in chain.into_iter().rev() {
            parent.append(sdk, pin, h).await?;
        }
        Ok(parent)
    }
    pub async fn append(&mut self, sdk: Vfs, pin: Pin, h: String) -> Result<()> {
        let overlay = Arc::new(OverlayFS::new_with_partial_origin_policy(
            self.fs.clone(),
            sdk.fs.clone(),
            policy(),
        ));
        overlay.load().await?;
        self.fs = overlay;
        self.pins.push(pin);
        self.artifacts.push(h);
        self.depth += 1;
        Ok(())
    }
}
pub async fn seal(store: &Path, sdk: &Vfs) -> Result<String> {
    let tmp = store
        .join("artifacts")
        .join(format!("{}.tmp", uuid::Uuid::new_v4()));
    sdk.snapshot_into(&tmp).await?;
    let (h, _) = hash_reader(File::open(&tmp)?)?;
    std::fs::rename(&tmp, store.join("artifacts").join(format!("{h}.db")))?;
    Ok(h)
}
pub fn artifact_pin(store: &Path, h: &str) -> Result<(Pin, PathBuf)> {
    digest(h)?;
    let p = store.join("artifacts").join(format!("{h}.db"));
    let f = open_shared_read(&p).with_context(|| format!("pin {h}"))?;
    ensure!(hash_reader(&f)?.0 == h, "artifact digest mismatch");
    Ok((f, p))
}
