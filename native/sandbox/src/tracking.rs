use crate::storage::{self, Image};
use async_trait::async_trait;
use std::{
    collections::{BTreeMap, BTreeSet},
    path::PathBuf,
    sync::{Arc, Mutex},
};
use tokio::sync::Mutex as AsyncMutex;
use vfs_core::fs::DirEntryPage;
use vfs_core::{
    error::{Error, Result},
    BoxedFile, DirEntry, File, FileSystem, FilesystemStats, Stats, TimeChange, WriteRange,
};

// Name-map locks never span an await. The capture mutex serializes first-touch evidence.
pub struct Tracking {
    pub inner: Arc<dyn FileSystem>,
    names: Mutex<BTreeMap<i64, BTreeSet<String>>>,
    pub before: AsyncMutex<BTreeMap<String, Image>>,
    record: PathBuf,
    cas: PathBuf,
    chunk_size: u64,
}
impl Tracking {
    pub fn new(
        inner: Arc<dyn FileSystem>,
        record: PathBuf,
        cas: PathBuf,
        before: BTreeMap<String, Image>,
        chunk_size: u64,
    ) -> Self {
        Self {
            inner,
            names: Mutex::new(BTreeMap::from([(1, BTreeSet::from([String::new()]))])),
            before: AsyncMutex::new(before),
            record,
            cas,
            chunk_size,
        }
    }
    fn child(&self, parent: i64, name: &str) -> String {
        let names = self.names.lock().unwrap();
        let p = names
            .get(&parent)
            .expect("parent path not resolved")
            .first()
            .unwrap();
        format!("{p}/{name}")
    }
    fn bind(&self, ino: i64, path: String) {
        self.names
            .lock()
            .unwrap()
            .entry(ino)
            .or_default()
            .insert(path);
    }
    pub(crate) async fn capture(&self, path: &str, range: Option<(u64, u64)>) -> Result<()> {
        let mut before = self.before.lock().await;
        let fresh = !before.contains_key(path);
        if fresh {
            let image = storage::metadata(self.inner.as_ref(), path, self.chunk_size).await?;
            // A newly resolved hard-link spelling shares the same Command preimage.
            let image = before
                .values()
                .find(|v| v.kind == "file" && image.kind == "file" && v.identity == image.identity)
                .cloned()
                .unwrap_or(image);
            before.insert(path.to_owned(), image);
        }
        let image = before.get_mut(path).unwrap();
        let old_blocks = image.blocks.len();
        let was_complete = image.complete;
        if image.kind == "file" {
            let (start, end) = range.unwrap_or((0, image.size));
            let first = start / self.chunk_size;
            let last = end.div_ceil(self.chunk_size);
            storage::capture_blocks(self.inner.as_ref(), path, &self.cas, image, first..last)
                .await?;
            if range.is_none() {
                image.complete = true;
                let size = image.size;
                image
                    .blocks
                    .retain(|index, _| index * self.chunk_size < size);
            }
        }
        if fresh || image.blocks.len() != old_blocks || image.complete != was_complete {
            storage::atomic_json(&self.record, &*before)
                .map_err(|e| Error::Internal(format!("persist before image: {e:#}")))?;
        }
        Ok(())
    }
    async fn touch(&self, ino: i64, range: Option<(u64, u64)>) -> Result<()> {
        let paths = self
            .names
            .lock()
            .unwrap()
            .get(&ino)
            .expect("write inode path not resolved")
            .clone();
        for path in paths {
            self.capture(&path, range).await?;
        }
        Ok(())
    }
    async fn namespace(&self, parent: i64, name: &str) -> Result<String> {
        let p = self.child(parent, name);
        self.capture(&p, None).await?;
        Ok(p)
    }
    async fn single_link(&self, parent: i64, name: &str) -> Result<()> {
        if self
            .inner
            .lookup(parent, name)
            .await?
            .is_some_and(|s| s.is_file() && s.nlink > 1)
        {
            return Err(std::io::Error::new(
                std::io::ErrorKind::Unsupported,
                "hard-link namespace changes are not supported by workspace evidence",
            )
            .into());
        }
        Ok(())
    }
    fn wrap(self: &Arc<Self>, ino: i64, file: BoxedFile) -> BoxedFile {
        Arc::new(TrackedFile {
            file,
            ino,
            tracker: self.clone(),
        })
    }
}
struct TrackedFile {
    file: BoxedFile,
    ino: i64,
    tracker: Arc<Tracking>,
}
impl TrackedFile {
    async fn capture_ranges(&self, ranges: &[WriteRange]) -> Result<()> {
        let size = self.file.fstat().await?.size as u64;
        for range in ranges.iter().filter(|range| !range.data.is_empty()) {
            let end = range
                .offset
                .checked_add(range.data.len() as u64)
                .expect("write range overflow");
            self.tracker
                .touch(self.ino, Some((range.offset.min(size), end)))
                .await?;
        }
        Ok(())
    }
}
#[async_trait]
impl File for TrackedFile {
    async fn pread(&self, o: u64, n: u64) -> Result<Vec<u8>> {
        self.file.pread(o, n).await
    }
    async fn pwrite(&self, o: u64, b: &[u8]) -> Result<()> {
        if b.is_empty() {
            return self.file.pwrite(o, b).await;
        }
        let size = self.file.fstat().await?.size as u64;
        let end = o.checked_add(b.len() as u64).expect("write range overflow");
        self.tracker
            .touch(self.ino, Some((o.min(size), end)))
            .await?;
        self.file.pwrite(o, b).await
    }
    async fn pwrite_ranges(&self, r: Vec<WriteRange>) -> Result<()> {
        self.capture_ranges(&r).await?;
        self.file.pwrite_ranges(r).await
    }
    async fn pwrite_ranges_batched(&self, r: Vec<WriteRange>) -> Result<()> {
        self.capture_ranges(&r).await?;
        self.file.pwrite_ranges_batched(r).await
    }
    async fn drain_writes(&self) -> Result<()> {
        self.file.drain_writes().await
    }
    async fn truncate(&self, n: u64) -> Result<()> {
        let old = self.file.fstat().await?.size as u64;
        self.tracker
            .touch(self.ino, Some((n.min(old), n.max(old))))
            .await?;
        self.file.truncate(n).await
    }
    async fn fsync(&self) -> Result<()> {
        self.file.fsync().await
    }
    async fn fstat(&self) -> Result<Stats> {
        self.file.fstat().await
    }
}
pub struct TrackingFS(pub Arc<Tracking>);
impl std::ops::Deref for TrackingFS {
    type Target = Arc<Tracking>;
    fn deref(&self) -> &Self::Target {
        &self.0
    }
}
// File handles retain their command's tracker independently of the mount's lifetime.
#[async_trait]
impl FileSystem for TrackingFS {
    fn names_equal(&self, a: &str, b: &str) -> bool {
        self.inner.names_equal(a, b)
    }
    fn file_identity(&self, i: i64) -> Result<String> {
        self.inner.file_identity(i)
    }
    fn kernel_cache_policy(&self, i: i64) -> vfs_core::fs::KernelCachePolicy {
        self.inner.kernel_cache_policy(i)
    }
    fn external_watch_root(&self) -> Option<PathBuf> {
        self.inner.external_watch_root()
    }
    fn external_watch_ignored_paths(&self) -> Vec<PathBuf> {
        self.inner.external_watch_ignored_paths()
    }
    async fn lookup(&self, p: i64, n: &str) -> Result<Option<Stats>> {
        Ok(self.lookup_named(p, n).await?.map(|e| e.stats))
    }
    async fn lookup_named(&self, p: i64, n: &str) -> Result<Option<DirEntry>> {
        let e = self.inner.lookup_named(p, n).await?;
        if let Some(ref e) = e {
            self.bind(e.stats.ino, self.child(p, &e.name));
        }
        Ok(e)
    }
    async fn getattr(&self, i: i64) -> Result<Option<Stats>> {
        self.inner.getattr(i).await
    }
    async fn readlink(&self, i: i64) -> Result<Option<String>> {
        self.inner.readlink(i).await
    }
    async fn readdir(&self, i: i64) -> Result<Option<Vec<String>>> {
        self.inner.readdir(i).await
    }
    async fn readdir_plus(&self, i: i64) -> Result<Option<Vec<DirEntry>>> {
        let r = self.inner.readdir_plus(i).await?;
        if let Some(ref entries) = r {
            for e in entries {
                self.bind(e.stats.ino, self.child(i, &e.name));
            }
        }
        Ok(r)
    }
    async fn readdir_plus_after(&self, i: i64, c: i64, n: usize) -> Result<Option<DirEntryPage>> {
        let r = self.inner.readdir_plus_after(i, c, n).await?;
        if let Some(ref page) = r {
            for e in &page.entries {
                self.bind(e.stats.ino, self.child(i, &e.name));
            }
        }
        Ok(r)
    }
    async fn chmod(&self, i: i64, m: u32) -> Result<()> {
        // Metadata only: an empty range records mode without reading file bytes.
        self.touch(i, Some((0, 0))).await?;
        self.inner.chmod(i, m).await
    }
    async fn chown(&self, i: i64, u: Option<u32>, g: Option<u32>) -> Result<()> {
        self.inner.chown(i, u, g).await
    }
    async fn utimens(&self, i: i64, a: TimeChange, m: TimeChange) -> Result<()> {
        self.inner.utimens(i, a, m).await
    }
    async fn open(&self, i: i64, f: i32) -> Result<BoxedFile> {
        if f & libc::O_TRUNC != 0 {
            self.touch(i, None).await?;
        }
        Ok(self.wrap(i, self.inner.open(i, f).await?))
    }
    async fn mkdir(&self, p: i64, n: &str, m: u32, u: u32, g: u32) -> Result<Stats> {
        let path = self.namespace(p, n).await?;
        let s = self.inner.mkdir(p, n, m, u, g).await?;
        self.bind(s.ino, path);
        Ok(s)
    }
    async fn create_file(
        &self,
        p: i64,
        n: &str,
        m: u32,
        u: u32,
        g: u32,
    ) -> Result<(Stats, BoxedFile)> {
        let path = self.namespace(p, n).await?;
        let (s, f) = self.inner.create_file(p, n, m, u, g).await?;
        self.bind(s.ino, path);
        let ino = s.ino;
        Ok((s, self.wrap(ino, f)))
    }
    async fn mknod(&self, p: i64, n: &str, m: u32, r: u64, u: u32, g: u32) -> Result<Stats> {
        let path = self.namespace(p, n).await?;
        let s = self.inner.mknod(p, n, m, r, u, g).await?;
        self.bind(s.ino, path);
        Ok(s)
    }
    async fn symlink(&self, p: i64, n: &str, t: &str, u: u32, g: u32) -> Result<Stats> {
        let path = self.namespace(p, n).await?;
        let s = self.inner.symlink(p, n, t, u, g).await?;
        self.bind(s.ino, path);
        Ok(s)
    }
    async fn unlink(&self, p: i64, n: &str) -> Result<()> {
        self.single_link(p, n).await?;
        self.namespace(p, n).await?;
        self.inner.unlink(p, n).await
    }
    async fn rmdir(&self, p: i64, n: &str) -> Result<()> {
        self.namespace(p, n).await?;
        self.inner.rmdir(p, n).await
    }
    async fn link(&self, i: i64, p: i64, n: &str) -> Result<Stats> {
        let path = self.namespace(p, n).await?;
        let s = self.inner.link(i, p, n).await?;
        self.bind(s.ino, path);
        Ok(s)
    }
    async fn rename(&self, p: i64, n: &str, q: i64, m: &str) -> Result<()> {
        self.rename_with_replaced_ino(p, n, q, m).await?;
        Ok(())
    }
    async fn rename_with_replaced_ino(
        &self,
        p: i64,
        n: &str,
        q: i64,
        m: &str,
    ) -> Result<Option<i64>> {
        // Evidence and host publication use paths as keys. Two spellings of the
        // same Windows name cannot be represented as independent path results.
        if p == q && n != m && self.inner.names_equal(n, m) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::Unsupported,
                "case-only rename is not supported by workspace evidence",
            )
            .into());
        }
        self.single_link(p, n).await?;
        self.single_link(q, m).await?;
        let old = self.namespace(p, n).await?;
        let new = self.namespace(q, m).await?;
        let ino = self.inner.lookup(p, n).await?.map(|s| s.ino);
        let replaced = self.inner.rename_with_replaced_ino(p, n, q, m).await?;
        if let Some(i) = ino {
            let mut names = self.names.lock().unwrap();
            let set = names.get_mut(&i).expect("rename inode unresolved");
            set.remove(&old);
            set.insert(new);
        }
        Ok(replaced)
    }
    async fn statfs(&self) -> Result<FilesystemStats> {
        self.inner.statfs().await
    }
    async fn drain_inode_writes(&self, i: i64) -> Result<()> {
        self.inner.drain_inode_writes(i).await
    }
    async fn drain_all(&self) -> Result<()> {
        self.inner.drain_all().await
    }
    async fn finalize(&self) -> Result<()> {
        self.inner.finalize().await
    }
    fn register_reap_hook(&self, h: Arc<dyn vfs_core::fs::vfs::ReapHook>) -> bool {
        self.inner.register_reap_hook(h)
    }
    async fn retain_lookup(&self, i: i64, n: u64) -> Result<()> {
        self.inner.retain_lookup(i, n).await
    }
    async fn forget(&self, i: i64, n: u64) {
        self.inner.forget(i, n).await
    }
}
