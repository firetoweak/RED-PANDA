//! Read-only Windows base using native handles and persistent file IDs.
//! Lookup is lazy; reparse points and host mutations are explicitly unsupported.
use crate::error::{Error, Result};
use crate::fs::{
    BoxedFile, DirEntry, File, FileSystem, FilesystemStats, FsError, KernelCachePolicy, Stats,
    TimeChange, S_IFDIR, S_IFREG,
};
use async_trait::async_trait;
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    ffi::OsString,
    fs::{File as NativeFile, OpenOptions},
    io,
    os::windows::{
        ffi::OsStringExt,
        fs::{FileExt, OpenOptionsExt},
        io::{AsRawHandle, FromRawHandle},
    },
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
};
use windows_sys::Win32::{
    Foundation::{GENERIC_READ, INVALID_HANDLE_VALUE},
    Storage::FileSystem::*,
};

const SHARE: u32 = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE;
const FLAGS: u32 = FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash)]
struct Identity {
    volume: u64,
    file: [u8; 16],
}
struct Node {
    file: NativeFile,
    id: Identity,
}
struct Entry {
    node: Arc<Node>,
    lookups: u64,
}
struct Cache {
    nodes: HashMap<i64, Entry>,
    identities: HashMap<Identity, i64>,
    next: i64,
}
#[derive(Default)]
struct Counters {
    metadata_opens: AtomicU64,
    directory_enumerations: AtomicU64,
    data_read_requests: AtomicU64,
    data_read_bytes: AtomicU64,
}

pub struct HostFS {
    root: PathBuf,
    cache: Mutex<Cache>,
    counters: Arc<Counters>,
}

fn query<T: Default>(file: &NativeFile, class: FILE_INFO_BY_HANDLE_CLASS) -> io::Result<T> {
    let mut info = T::default();
    // SAFETY: each caller pairs the documented information class and repr(C) struct.
    let ok = unsafe {
        GetFileInformationByHandleEx(
            file.as_raw_handle(),
            class,
            (&mut info as *mut T).cast(),
            std::mem::size_of::<T>() as u32,
        )
    };
    if ok == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(info)
}

fn identity(file: &NativeFile) -> io::Result<Identity> {
    let info: FILE_ID_INFO = query(file, FileIdInfo)?;
    Ok(Identity {
        volume: info.VolumeSerialNumber,
        file: info.FileId.Identifier,
    })
}

fn stats(file: &NativeFile, ino: i64) -> Result<Stats> {
    let basic: FILE_BASIC_INFO = query(file, FileBasicInfo)?;
    let standard: FILE_STANDARD_INFO = query(file, FileStandardInfo)?;
    if basic.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "Windows HostFS does not follow reparse points",
        )
        .into());
    }
    let (atime, atime_nsec) = crate::fs::base_fingerprint::windows_timestamp(basic.LastAccessTime);
    let fingerprint =
        crate::fs::base_fingerprint::BaseFingerprint::from_windows_info(&basic, &standard);
    let (mtime, mtime_nsec) = (fingerprint.mtime, fingerprint.mtime_nsec as u32);
    let (ctime, ctime_nsec) = (fingerprint.ctime, fingerprint.ctime_nsec as u32);
    Ok(Stats {
        ino,
        mode: if standard.Directory {
            S_IFDIR | 0o755
        } else {
            S_IFREG
                | if basic.FileAttributes & FILE_ATTRIBUTE_READONLY != 0 {
                    0o444
                } else {
                    0o644
                }
        },
        nlink: standard.NumberOfLinks,
        uid: 0,
        gid: 0,
        size: standard.EndOfFile,
        atime,
        atime_nsec,
        mtime,
        mtime_nsec,
        ctime,
        ctime_nsec,
        rdev: 0,
    })
}

fn current_path(file: &NativeFile) -> Result<PathBuf> {
    let needed =
        unsafe { GetFinalPathNameByHandleW(file.as_raw_handle(), std::ptr::null_mut(), 0, 0) };
    if needed == 0 {
        return Err(io::Error::last_os_error().into());
    }
    let mut buffer = vec![0u16; needed as usize];
    let length =
        unsafe { GetFinalPathNameByHandleW(file.as_raw_handle(), buffer.as_mut_ptr(), needed, 0) };
    if length == 0 {
        return Err(io::Error::last_os_error().into());
    }
    if length >= needed {
        return Err(io::Error::new(
            io::ErrorKind::Interrupted,
            "directory path changed during query",
        )
        .into());
    }
    Ok(PathBuf::from(OsString::from_wide(
        &buffer[..length as usize],
    )))
}

fn component(name: &str) -> Result<()> {
    if name.is_empty() || name == "." || name == ".." || name.contains(['/', '\\', ':', '\0']) {
        return Err(FsError::InvalidPath.into());
    }
    Ok(())
}

fn metadata_handle(path: &Path) -> io::Result<NativeFile> {
    OpenOptions::new()
        .access_mode(FILE_READ_ATTRIBUTES)
        .share_mode(SHARE)
        .custom_flags(FLAGS)
        .open(path)
}

impl HostFS {
    pub fn new(root: impl AsRef<Path>) -> Result<Self> {
        let root = std::path::absolute(root)?;
        let file = metadata_handle(&root)?;
        if !stats(&file, 1)?.is_directory() {
            return Err(FsError::NotADirectory.into());
        }
        let id = identity(&file)?;
        let node = Arc::new(Node { file, id });
        let counters = Arc::new(Counters::default());
        counters.metadata_opens.store(1, Ordering::Relaxed);
        Ok(Self {
            root,
            counters,
            cache: Mutex::new(Cache {
                nodes: HashMap::from([(1, Entry { node, lookups: 1 })]),
                identities: HashMap::from([(id, 1)]),
                next: 2,
            }),
        })
    }
    fn node(&self, ino: i64) -> Result<Arc<Node>> {
        self.cache
            .lock()
            .unwrap()
            .nodes
            .get(&ino)
            .map(|e| e.node.clone())
            .ok_or_else(|| FsError::NotFound.into())
    }
    pub fn observations(&self) -> Value {
        json!({"cached_inodes":self.cache.lock().unwrap().nodes.len(),
            "metadata_opens":self.counters.metadata_opens.load(Ordering::Relaxed),
            "directory_enumerations":self.counters.directory_enumerations.load(Ordering::Relaxed),
            "data_read_requests":self.counters.data_read_requests.load(Ordering::Relaxed),
            "data_read_bytes":self.counters.data_read_bytes.load(Ordering::Relaxed)})
    }
}

struct ReadFile {
    file: Arc<NativeFile>,
    ino: i64,
    counters: Arc<Counters>,
}

#[async_trait]
impl File for ReadFile {
    async fn pread(&self, offset: u64, size: u64) -> Result<Vec<u8>> {
        let file = self.file.clone();
        let counters = self.counters.clone();
        tokio::task::spawn_blocking(move || {
            counters.data_read_requests.fetch_add(1, Ordering::Relaxed);
            let mut data = vec![0; size as usize];
            let mut filled = 0;
            while filled < data.len() {
                let n = file.seek_read(&mut data[filled..], offset + filled as u64)?;
                if n == 0 {
                    break;
                }
                filled += n;
                counters
                    .data_read_bytes
                    .fetch_add(n as u64, Ordering::Relaxed);
            }
            data.truncate(filled);
            Ok::<_, Error>(data)
        })
        .await
        .map_err(|error| Error::Internal(error.to_string()))?
    }
    async fn pwrite(&self, _: u64, _: &[u8]) -> Result<()> {
        Err(FsError::OperationNotPermitted.into())
    }
    async fn truncate(&self, _: u64) -> Result<()> {
        Err(FsError::OperationNotPermitted.into())
    }
    async fn fsync(&self) -> Result<()> {
        Ok(())
    } // No dirty data exists on this read-only handle.
    async fn fstat(&self) -> Result<Stats> {
        stats(&self.file, self.ino)
    }
}

#[async_trait]
impl FileSystem for HostFS {
    fn file_identity(&self, ino: i64) -> Result<String> {
        let id = self.node(ino)?.id;
        let bytes: Vec<u8> = id.volume.to_le_bytes().into_iter().chain(id.file).collect();
        Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
    }

    fn names_equal(&self, left: &str, right: &str) -> bool {
        if left == right {
            return true;
        }
        let left: Vec<u16> = left.encode_utf16().collect();
        let right: Vec<u16> = right.encode_utf16().collect();
        let result = unsafe {
            windows_sys::Win32::Globalization::CompareStringOrdinal(
                left.as_ptr(),
                left.len().try_into().unwrap(),
                right.as_ptr(),
                right.len().try_into().unwrap(),
                1,
            )
        };
        assert_ne!(
            result,
            0,
            "CompareStringOrdinal failed: {}",
            io::Error::last_os_error()
        );
        result == 2
    }
    async fn lookup_named(&self, parent: i64, name: &str) -> Result<Option<DirEntry>> {
        component(name)?;
        let parent_node = self.node(parent)?;
        let file = match metadata_handle(&current_path(&parent_node.file)?.join(name)) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        self.counters.metadata_opens.fetch_add(1, Ordering::Relaxed);
        stats(&file, 0)?; // Reject a reparse entry before resolving its display spelling.
        let path = current_path(&file)?;
        let actual = path
            .file_name()
            .unwrap()
            .to_str()
            .ok_or_else(|| Error::InvalidUtf8Path(path.to_string_lossy().into_owned()))?
            .to_owned();
        let stats = self
            .lookup(parent, &actual)
            .await?
            .ok_or(FsError::NotFound)?;
        Ok(Some(DirEntry {
            name: actual,
            stats,
            cookie: 0,
        }))
    }

    async fn lookup(&self, parent: i64, name: &str) -> Result<Option<Stats>> {
        component(name)?;
        let parent = self.node(parent)?;
        if !stats(&parent.file, 0)?.is_directory() {
            return Err(FsError::NotADirectory.into());
        }
        let path = current_path(&parent.file)?.join(name);
        let file = match metadata_handle(&path) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        self.counters.metadata_opens.fetch_add(1, Ordering::Relaxed);
        let mut attributes = stats(&file, 0)?;
        let id = identity(&file)?;
        let mut cache = self.cache.lock().unwrap();
        let ino = if let Some(&ino) = cache.identities.get(&id) {
            cache.nodes.get_mut(&ino).unwrap().lookups += 1;
            ino
        } else {
            let ino = cache.next;
            cache.next += 1;
            cache.identities.insert(id, ino);
            cache.nodes.insert(
                ino,
                Entry {
                    node: Arc::new(Node { file, id }),
                    lookups: 1,
                },
            );
            ino
        };
        attributes.ino = ino;
        Ok(Some(attributes))
    }
    async fn getattr(&self, ino: i64) -> Result<Option<Stats>> {
        let node = self
            .cache
            .lock()
            .unwrap()
            .nodes
            .get(&ino)
            .map(|e| e.node.clone());
        node.map(|node| stats(&node.file, ino)).transpose()
    }
    async fn readlink(&self, ino: i64) -> Result<Option<String>> {
        self.node(ino)?;
        Ok(None)
    }
    async fn readdir(&self, ino: i64) -> Result<Option<Vec<String>>> {
        let node = self.node(ino)?;
        if !stats(&node.file, ino)?.is_directory() {
            return Err(FsError::NotADirectory.into());
        }
        self.counters
            .directory_enumerations
            .fetch_add(1, Ordering::Relaxed);
        let mut names = Vec::new();
        for entry in std::fs::read_dir(current_path(&node.file)?)? {
            let name = entry?
                .file_name()
                .into_string()
                .map_err(|name| Error::InvalidUtf8Path(name.to_string_lossy().into_owned()))?;
            names.push(name);
        }
        names.sort();
        Ok(Some(names))
    }
    async fn readdir_plus(&self, ino: i64) -> Result<Option<Vec<DirEntry>>> {
        let mut result = Vec::new();
        for (index, name) in self.readdir(ino).await?.unwrap().into_iter().enumerate() {
            if let Some(stats) = self.lookup(ino, &name).await? {
                result.push(DirEntry {
                    name,
                    stats,
                    cookie: index as i64 + 1,
                });
            }
        }
        Ok(Some(result))
    }
    async fn open(&self, ino: i64, flags: i32) -> Result<BoxedFile> {
        if flags & (libc::O_WRONLY | libc::O_RDWR | libc::O_TRUNC | libc::O_APPEND | libc::O_CREAT)
            != 0
        {
            return Err(FsError::OperationNotPermitted.into());
        }
        let node = self.node(ino)?;
        if stats(&node.file, ino)?.is_directory() {
            return Err(FsError::IsADirectory.into());
        }
        let handle = unsafe {
            ReOpenFile(
                node.file.as_raw_handle(),
                GENERIC_READ,
                SHARE,
                FILE_FLAG_OPEN_REPARSE_POINT,
            )
        };
        if handle == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error().into());
        }
        let file = unsafe { NativeFile::from_raw_handle(handle) };
        Ok(Arc::new(ReadFile {
            file: Arc::new(file),
            ino,
            counters: self.counters.clone(),
        }))
    }
    fn kernel_cache_policy(&self, _: i64) -> KernelCachePolicy {
        KernelCachePolicy::ExternalDrift
    }
    fn external_watch_root(&self) -> Option<PathBuf> {
        Some(self.root.clone())
    }
    async fn retain_lookup(&self, ino: i64, count: u64) -> Result<()> {
        self.cache
            .lock()
            .unwrap()
            .nodes
            .get_mut(&ino)
            .ok_or(FsError::NotFound)?
            .lookups += count;
        Ok(())
    }
    async fn forget(&self, ino: i64, count: u64) {
        if ino == 1 {
            return;
        }
        let mut cache = self.cache.lock().unwrap();
        let entry = cache.nodes.get_mut(&ino).unwrap();
        entry.lookups -= count;
        if entry.lookups == 0 {
            let entry = cache.nodes.remove(&ino).unwrap();
            cache.identities.remove(&entry.node.id);
        }
    }
    async fn chmod(&self, _: i64, _: u32) -> Result<()> {
        Err(FsError::OperationNotPermitted.into())
    }
    async fn chown(&self, _: i64, _: Option<u32>, _: Option<u32>) -> Result<()> {
        Err(FsError::OperationNotPermitted.into())
    }
    async fn utimens(&self, _: i64, _: TimeChange, _: TimeChange) -> Result<()> {
        Err(FsError::OperationNotPermitted.into())
    }
    async fn mkdir(&self, _: i64, _: &str, _: u32, _: u32, _: u32) -> Result<Stats> {
        Err(FsError::OperationNotPermitted.into())
    }
    async fn create_file(
        &self,
        _: i64,
        _: &str,
        _: u32,
        _: u32,
        _: u32,
    ) -> Result<(Stats, BoxedFile)> {
        Err(FsError::OperationNotPermitted.into())
    }
    async fn mknod(&self, _: i64, _: &str, _: u32, _: u64, _: u32, _: u32) -> Result<Stats> {
        Err(FsError::OperationNotPermitted.into())
    }
    async fn symlink(&self, _: i64, _: &str, _: &str, _: u32, _: u32) -> Result<Stats> {
        Err(FsError::OperationNotPermitted.into())
    }
    async fn unlink(&self, _: i64, _: &str) -> Result<()> {
        Err(FsError::OperationNotPermitted.into())
    }
    async fn rmdir(&self, _: i64, _: &str) -> Result<()> {
        Err(FsError::OperationNotPermitted.into())
    }
    async fn link(&self, _: i64, _: i64, _: &str) -> Result<Stats> {
        Err(FsError::OperationNotPermitted.into())
    }
    async fn rename(&self, _: i64, _: &str, _: i64, _: &str) -> Result<()> {
        Err(FsError::OperationNotPermitted.into())
    }
    async fn statfs(&self) -> Result<FilesystemStats> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "Windows HostFS volume statistics are unsupported",
        )
        .into())
    }
}
