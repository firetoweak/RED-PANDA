mod restore;
mod storage;
mod tracking;
use anyhow::{ensure, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
#[cfg(unix)]
use std::os::unix::io::AsRawFd;
#[cfg(windows)]
use std::os::windows::fs::OpenOptionsExt;
use std::{
    collections::BTreeMap,
    fs::{File, OpenOptions},
    io::{BufRead, Write},
    path::{Path, PathBuf},
    sync::Arc,
    time::Instant,
};
use storage::{Image, Parent};
use tracking::{Tracking, TrackingFS};
use vfs_core::{OverlayFS, Vfs, VfsOptions};
use vfs_mount::{mount_fs, Backend, MountHandle, MountOpts};

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    version: u32,
    base: PathBuf,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Commit {
    previous: Option<String>,
    digest: Option<String>,
    command_id: Option<String>,
    kind: String,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Begin {
    command_id: String,
    parent_commit: Option<String>,
    parent_digest: Option<String>,
    operation: restore::Operation,
}
#[derive(Serialize, Deserialize, Clone)]
#[serde(deny_unknown_fields)]
struct Change {
    path: String,
    before: Image,
    after: Image,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Sealed {
    version: u32,
    command_id: String,
    parent_commit: Option<String>,
    digest: String,
    changes: Vec<Change>,
    operation: restore::Operation,
}
#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Request {
    Begin {
        command_id: String,
    },
    Finish {},
    Recover {
        command_id: String,
    },
    Accept {
        command_id: String,
    },
    Restore {
        command_id: String,
        targets: Vec<String>,
        policy: restore::Policy,
    },
    Discard {
        command_id: String,
    },
    Status {
        command_id: String,
    },
    Inspect {
        path: String,
    },
    Info {},
    Prune {},
    Pause {},
    Rebase {
        expected_commit: String,
    },
    Shutdown {},
}
struct Candidate {
    id: String,
    sdk: Vfs,
    fs: Arc<Tracking>,
    mount: Option<MountHandle>,
}
struct Service {
    store: PathBuf,
    config: Config,
    tip: Option<String>,
    digest: Option<String>,
    parent: Parent,
    active: Option<Candidate>,
    paused: bool,
    _owner: File,
}
#[cfg(not(any(windows, target_os = "linux")))]
compile_error!("redpanda-sandbox mounts on Windows (WinFsp) and Linux (FUSE)");

fn local_path(path: &Path) -> Result<PathBuf> {
    let p = path.canonicalize()?;
    #[cfg(windows)]
    {
        let value = p.to_str().context("UTF-8 local path")?;
        let value = value.strip_prefix(r"\\?\").unwrap_or(value);
        ensure!(
            value.as_bytes().get(1) == Some(&b':'),
            "a local drive path is required"
        );
        Ok(PathBuf::from(value))
    }
    #[cfg(unix)]
    {
        ensure!(p.is_absolute(), "an absolute path is required");
        let _ = p.to_str().context("UTF-8 local path")?;
        Ok(p)
    }
}
fn lock_owner(path: &Path) -> Result<File> {
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true).truncate(false);
    #[cfg(windows)]
    options.share_mode(0);
    let file = options.open(path).context("owner lock")?;
    #[cfg(unix)]
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err(std::io::Error::last_os_error()).context("owner lock");
    }
    Ok(file)
}
fn mount_backend() -> Backend {
    #[cfg(windows)]
    {
        Backend::WinFsp
    }
    #[cfg(target_os = "linux")]
    {
        Backend::Fuse
    }
}
fn unsupported(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| {
        cause
            .downcast_ref::<std::io::Error>()
            .is_some_and(|io| io.kind() == std::io::ErrorKind::Unsupported)
    })
}
#[cfg(target_os = "linux")]
fn clear_stale_mount(point: &Path) -> Result<()> {
    match std::fs::read_dir(point) {
        Err(error) if error.raw_os_error() == Some(libc::ENOTCONN) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error.into()),
        Ok(_) => return Ok(()),
    }
    let status = std::process::Command::new("fusermount3")
        .args(["-u", "-z"])
        .arg(point)
        .status()
        .context("fusermount3")?;
    ensure!(
        status.success(),
        "fusermount3 failed to clear a stale mount"
    );
    Ok(())
}
fn artifact_is_busy(error: &std::io::Error) -> bool {
    match error.raw_os_error() {
        #[cfg(windows)]
        Some(32) => true,
        #[cfg(unix)]
        Some(libc::EBUSY) => true,
        _ => false,
    }
}
impl Service {
    async fn open(store: PathBuf, base: PathBuf) -> Result<Self> {
        std::fs::create_dir_all(&store)?;
        let store = local_path(&store)?;
        let base = local_path(&base)?;
        ensure!(
            base.is_dir() && !store.starts_with(&base) && !base.starts_with(&store),
            "store and base must be disjoint directories"
        );
        // Native database and mount APIs need a verbatim path beyond MAX_PATH.
        let store = store.canonicalize()?;
        let owner = lock_owner(&store.join("owner.lock"))?;
        let config_path = store.join("config.json");
        let config = if config_path.exists() {
            let c: Config = storage::read_json(&config_path)?;
            ensure!(c.version == 3 && c.base == base, "workspace config differs");
            c
        } else {
            let c = Config { version: 3, base };
            for p in ["commands", "artifacts", "cas", "commits"] {
                std::fs::create_dir(store.join(p))?;
            }
            storage::durable(&config_path, &serde_json::to_vec(&c)?)?;
            c
        };
        let tip = if store.join("HEAD").exists() {
            Some(std::fs::read_to_string(store.join("HEAD"))?)
        } else {
            None
        };
        let digest = match &tip {
            Some(id) => Self::commit_at(&store, id)?.digest,
            None => None,
        };
        let parent = Parent::cold(&store, &config.base, digest.as_deref()).await?;
        Ok(Self {
            store,
            config,
            tip,
            digest,
            parent,
            active: None,
            paused: false,
            _owner: owner,
        })
    }
    fn command(&self, id: &str) -> Result<PathBuf> {
        storage::identifier(id)?;
        Ok(self.store.join("commands").join(id))
    }
    fn commit_at(store: &Path, id: &str) -> Result<Commit> {
        ensure!(
            uuid::Uuid::parse_str(id)?.to_string() == id,
            "invalid commit id"
        );
        let c: Commit = storage::read_json(&store.join("commits").join(format!("{id}.json")))?;
        if let Some(previous) = &c.previous {
            ensure!(
                uuid::Uuid::parse_str(previous)?.to_string() == *previous,
                "invalid previous commit"
            );
        }
        match c.kind.as_str() {
            "accept" => {
                storage::identifier(
                    c.command_id
                        .as_deref()
                        .context("accepted command missing")?,
                )?;
                storage::digest(c.digest.as_deref().context("accepted digest missing")?)?;
            }
            "rebase" => ensure!(
                c.digest.is_none() && c.command_id.is_none(),
                "invalid rebase commit"
            ),
            _ => anyhow::bail!("invalid commit kind"),
        }
        Ok(c)
    }
    fn accepted(&self, id: &str) -> Result<bool> {
        let mut next = self.tip.clone();
        let mut seen = std::collections::HashSet::new();
        while let Some(h) = next {
            ensure!(seen.insert(h.clone()), "cyclic commit history");
            let c = Self::commit_at(&self.store, &h)?;
            if c.command_id.as_deref() == Some(id) {
                return Ok(true);
            }
            next = c.previous;
        }
        Ok(false)
    }
    fn commit(&mut self, digest: Option<String>, id: Option<String>, kind: &str) -> Result<()> {
        let token = uuid::Uuid::new_v4().to_string();
        let c = Commit {
            previous: self.tip.clone(),
            digest: digest.clone(),
            command_id: id,
            kind: kind.to_owned(),
        };
        storage::durable(
            &self.store.join("commits").join(format!("{token}.json")),
            &serde_json::to_vec(&c)?,
        )?;
        let temp = self.store.join(format!("HEAD.{token}.next"));
        storage::durable(&temp, token.as_bytes())?;
        storage::replace(&temp, &self.store.join("HEAD"))?;
        self.tip = Some(token);
        self.digest = digest;
        Ok(())
    }
    async fn begin(&mut self, id: String) -> Result<Value> {
        if self.paused {
            return Ok(json!({"status":"paused"}));
        }
        if self.active.is_some() {
            return Ok(json!({"status":"busy"}));
        }
        let dir = self.command(&id)?;
        if dir.exists() {
            return Ok(json!({"status":"existing_command","receipt":self.status(&id)?}));
        }
        let start = Instant::now();
        let mut candidate = self
            .create_candidate(id, restore::Operation::Execute)
            .await?;
        // One active candidate owns this point; Command directories hold evidence only.
        let point = local_path(&self.store)?.join("mount");
        #[cfg(target_os = "linux")]
        clear_stale_mount(&point)?;
        #[cfg(unix)]
        std::fs::create_dir_all(&point)?;
        candidate.mount = Some(
            mount_fs(
                Arc::new(TrackingFS(candidate.fs.clone())),
                MountOpts::new(point.clone(), mount_backend()),
            )
            .await?,
        );
        self.active = Some(candidate);
        Ok(
            json!({"status":"active","mount":point,"parent_depth":self.parent.depth,"parent_hashed_bytes":0,"begin_ms":start.elapsed().as_secs_f64()*1000.0}),
        )
    }
    async fn create_candidate(
        &self,
        id: String,
        operation: restore::Operation,
    ) -> Result<Candidate> {
        let dir = self.command(&id)?;
        operation.validate()?;
        std::fs::create_dir(&dir)?;
        storage::durable(
            &dir.join("begin.json"),
            &serde_json::to_vec(&Begin {
                command_id: id.clone(),
                parent_commit: self.tip.clone(),
                parent_digest: self.digest.clone(),
                operation,
            })?,
        )?;
        storage::durable(&dir.join("before.json"), b"{}")?;
        let sdk = Vfs::open(VfsOptions::with_path(
            dir.join("delta.db").to_string_lossy(),
        ))
        .await?;
        let view = Arc::new(OverlayFS::new_with_partial_origin_policy(
            self.parent.fs.clone(),
            sdk.fs.clone(),
            storage::policy(),
        ));
        view.init(self.config.base.to_str().context("UTF-8 base")?)
            .await?;
        if let Some(ref h) = self.digest {
            sdk.set_overlay_parent_artifact(h).await?;
        }
        let tracker = Arc::new(Tracking::new(
            view,
            dir.join("before.json"),
            self.store.join("cas"),
            BTreeMap::new(),
            sdk.fs.chunk_size() as u64,
        ));
        Ok(Candidate {
            id,
            sdk,
            fs: tracker,
            mount: None,
        })
    }
    async fn finish_candidate(&self, id: &str, sdk: Vfs, fs: Arc<Tracking>) -> Result<Value> {
        let dir = self.command(id)?;
        let begin: Begin = storage::read_json(&dir.join("begin.json"))?;
        ensure!(begin.parent_commit == self.tip, "candidate parent changed");
        begin.operation.validate()?;
        if matches!(begin.operation, restore::Operation::Restore { .. }) {
            let completed: restore::Completed =
                storage::read_json(&dir.join("restore-complete.json"))?;
            ensure!(
                completed.command_id == id && completed.parent_commit == begin.parent_commit,
                "restore completion differs"
            );
        }
        fs.inner.finalize().await?;
        let mut changes = vec![];
        for (path, before) in &*fs.before.lock().await {
            let after =
                storage::after_image(fs.inner.as_ref(), path, &self.store.join("cas"), before)
                    .await?;
            if before.kind != after.kind
                || before.size != after.size
                || before.blocks != after.blocks
                || before.identity != after.identity
                || before.mode != after.mode
                || before.target != after.target
            {
                changes.push(Change {
                    path: path.clone(),
                    before: before.clone(),
                    after,
                });
            }
        }
        let h = storage::seal(&self.store, &sdk).await?;
        let sealed = Sealed {
            version: 3,
            command_id: id.to_owned(),
            parent_commit: begin.parent_commit,
            digest: h,
            changes,
            operation: begin.operation,
        };
        storage::durable(&dir.join("sealed.json"), &serde_json::to_vec(&sealed)?)?;
        Ok(json!({"status":"sealed","receipt":sealed}))
    }
    async fn finish(&mut self) -> Result<Value> {
        let Some(c) = self.active.take() else {
            return Ok(json!({"status":"no_active_command"}));
        };
        if let Some(mount) = c.mount {
            mount.unmount().await?;
        }
        match self.finish_candidate(&c.id, c.sdk, c.fs).await {
            Ok(value) => Ok(value),
            Err(error) if unsupported(&error) => {
                let reason = format!("{error:#}");
                storage::atomic_json(
                    &self.command(&c.id)?.join("rejected.json"),
                    &json!({"error": reason}),
                )?;
                Ok(json!({"status":"rejected","command_id":c.id,"error":reason}))
            }
            Err(error) => Err(error),
        }
    }
    async fn recover(&mut self, id: &str) -> Result<Value> {
        if self.active.is_some() {
            return Ok(json!({"status":"busy"}));
        }
        let dir = self.command(id)?;
        if dir.join("sealed.json").exists() {
            return self.status(id);
        }
        if dir.join("rejected.json").exists() {
            return self.status(id);
        }
        let begin: Begin = storage::read_json(&dir.join("begin.json"))?;
        ensure!(begin.parent_commit == self.tip, "recovery parent changed");
        begin.operation.validate()?;
        if matches!(begin.operation, restore::Operation::Restore { .. })
            && !dir.join("restore-complete.json").exists()
        {
            return Ok(json!({"status":"incomplete_restore","command_id":id}));
        }
        let sdk = Vfs::open(VfsOptions::with_path(
            dir.join("delta.db").to_string_lossy(),
        ))
        .await?;
        ensure!(
            sdk.overlay_parent_artifact().await? == begin.parent_digest,
            "recovery parent artifact changed"
        );
        let overlay = Arc::new(OverlayFS::new_with_partial_origin_policy(
            self.parent.fs.clone(),
            sdk.fs.clone(),
            storage::policy(),
        ));
        overlay.load().await?;
        let before = storage::read_json(&dir.join("before.json"))?;
        let fs = Arc::new(Tracking::new(
            overlay,
            dir.join("before.json"),
            self.store.join("cas"),
            before,
            sdk.fs.chunk_size() as u64,
        ));
        self.finish_candidate(id, sdk, fs).await
    }
    fn status(&self, id: &str) -> Result<Value> {
        let dir = self.command(id)?;
        if !dir.exists() {
            return Ok(json!({"status":"missing"}));
        }
        let state = if self.accepted(id)? {
            "accepted"
        } else if dir.join("discarded.json").exists() {
            "discarded"
        } else if dir.join("sealed.json").exists() {
            "sealed"
        } else if dir.join("rejected.json").exists() {
            "rejected"
        } else {
            "unknown"
        };
        let receipt = if dir.join("sealed.json").exists() {
            Some(storage::read_json::<Sealed>(&dir.join("sealed.json"))?)
        } else {
            None
        };
        Ok(json!({"status":state,"receipt":receipt}))
    }
    async fn accept(&mut self, id: &str) -> Result<Value> {
        if self.paused {
            return Ok(json!({"status":"paused"}));
        }
        if self.active.is_some() {
            return Ok(json!({"status":"busy"}));
        }
        if self.accepted(id)? {
            return self.status(id);
        }
        let dir = self.command(id)?;
        if dir.join("rejected.json").exists() {
            return self.status(id);
        }
        if !dir.join("sealed.json").exists() {
            return Ok(json!({"status":"not_sealed","command_id":id}));
        }
        ensure!(
            !dir.join("discarded.json").exists(),
            "discarded command cannot be accepted"
        );
        let sealed: Sealed = storage::read_json(&dir.join("sealed.json"))?;
        ensure!(
            sealed.version == 3 && sealed.command_id == id,
            "invalid sealed receipt"
        );
        sealed.operation.validate()?;
        if sealed.parent_commit != self.tip {
            return Ok(json!({"status":"parent_changed"}));
        }
        let (pin, path) = storage::artifact_pin(&self.store, &sealed.digest)?;
        let sdk = Vfs::open_read_only(path).await?;
        let mut parent = Parent {
            fs: self.parent.fs.clone(),
            host: self.parent.host.clone(),
            pins: vec![],
            artifacts: vec![],
            depth: self.parent.depth,
            hashed_bytes: 0,
        };
        parent.append(sdk, pin, sealed.digest.clone()).await?;
        self.commit(Some(sealed.digest), Some(id.to_owned()), "accept")?;
        self.parent.fs = parent.fs;
        self.parent.pins.extend(parent.pins);
        self.parent.depth = parent.depth;
        self.parent.artifacts.extend(parent.artifacts);
        self.status(id)
    }
    async fn restore(
        &mut self,
        id: String,
        targets: Vec<String>,
        policy: restore::Policy,
    ) -> Result<Value> {
        let operation = restore::Operation::Restore {
            targets: targets.clone(),
            policy: policy.clone(),
        };
        if let Err(error) = storage::identifier(&id).and_then(|()| operation.validate()) {
            return Ok(json!({"status":"invalid_request","error":error.to_string()}));
        }
        let dir = self.command(&id)?;
        if dir.exists() {
            let begin: Begin = storage::read_json(&dir.join("begin.json"))?;
            begin.operation.validate()?;
            if begin.operation != operation {
                return Ok(json!({"status":"request_mismatch"}));
            }
            return self.status(&id);
        }
        if self.paused {
            return Ok(json!({"status":"paused"}));
        }
        if self.active.is_some() {
            return Ok(json!({"status":"busy"}));
        }
        let mut next = self.tip.clone();
        let mut seen = std::collections::HashSet::new();
        let mut receipts = Vec::new();
        while let Some(token) = next {
            ensure!(seen.insert(token.clone()), "cyclic commit history");
            let commit = Self::commit_at(&self.store, &token)?;
            if let Some(command_id) = commit.command_id {
                if targets[targets.len() - receipts.len() - 1] != command_id {
                    return Ok(json!({"status":"range_changed"}));
                }
                let receipt: Sealed =
                    storage::read_json(&self.command(&command_id)?.join("sealed.json"))?;
                ensure!(
                    receipt.command_id == command_id
                        && receipt.parent_commit == commit.previous
                        && Some(&receipt.digest) == commit.digest.as_ref()
                        && receipt.version == 3,
                    "accepted receipt differs"
                );
                receipt.operation.validate()?;
                receipts.push(receipt);
                if receipts.len() == targets.len() {
                    break;
                }
            }
            next = commit.previous;
        }
        if receipts.len() != targets.len() {
            return Ok(json!({"status":"range_changed"}));
        }
        receipts.reverse();
        let cas = self.store.join("cas");
        let bindings = restore::bindings(&self.store)?;
        let plan = match restore::plan(self.parent.fs.as_ref(), &cas, &receipts, &policy, &bindings)
            .await?
        {
            Ok(plan) => plan,
            Err(conflict) => {
                return Ok(
                    json!({"status":"conflict","reason":conflict.reason,"path":conflict.path}),
                )
            }
        };
        let candidate = self.create_candidate(id.clone(), operation).await?;
        // The owner uses only the core filesystem, never its own native mount.
        restore::apply(&TrackingFS(candidate.fs.clone()), &cas, &plan).await?;
        storage::durable(
            &dir.join("restore-complete.json"),
            &serde_json::to_vec(&restore::Completed {
                command_id: id.clone(),
                parent_commit: self.tip.clone(),
            })?,
        )?;
        self.finish_candidate(&id, candidate.sdk, candidate.fs)
            .await
    }
    async fn request(&mut self, r: Request) -> Result<Value> {
        match r {
            Request::Begin { command_id } => self.begin(command_id).await,
            Request::Finish {} => self.finish().await,
            Request::Recover { command_id } => self.recover(&command_id).await,
            Request::Accept { command_id } => self.accept(&command_id).await,
            Request::Restore {
                command_id,
                targets,
                policy,
            } => self.restore(command_id, targets, policy).await,
            Request::Discard { command_id } => {
                ensure!(
                    !self.accepted(&command_id)?,
                    "accepted command cannot be discarded"
                );
                ensure!(self.active.is_none(), "finish execution before discard");
                let d = self.command(&command_id)?;
                storage::atomic_json(&d.join("discarded.json"), &json!({"command_id":command_id}))?;
                self.status(&command_id)
            }
            Request::Status { command_id } => self.status(&command_id),
            Request::Inspect { path } => {
                ensure!(
                    self.active.is_none() && !self.paused,
                    "inspect requires an idle attached view"
                );
                Ok(
                    json!({"image":storage::metadata(self.parent.fs.as_ref(),&path,vfs_core::config::DEFAULT_CHUNK_SIZE as u64).await?}),
                )
            }
            Request::Info {} => Ok(
                json!({"version":3,"commit":self.tip,"digest":self.digest,"depth":self.parent.depth,"cold_hashed_bytes":self.parent.hashed_bytes,"host":self.parent.host.observations(),"paused":self.paused,"active":self.active.as_ref().map(|c|&c.id)}),
            ),
            Request::Prune {} => {
                ensure!(
                    self.active.is_none() && !self.paused,
                    "prune requires idle attached view"
                );
                let mut keep: std::collections::HashSet<_> =
                    self.parent.artifacts.iter().cloned().collect();
                for item in std::fs::read_dir(self.store.join("commands"))? {
                    let dir = item?.path();
                    let id = dir
                        .file_name()
                        .and_then(|s| s.to_str())
                        .context("command directory name")?;
                    if dir.join("sealed.json").exists()
                        && !dir.join("discarded.json").exists()
                        && !self.accepted(id)?
                    {
                        let s: Sealed = storage::read_json(&dir.join("sealed.json"))?;
                        let p =
                            Parent::cold(&self.store, &self.config.base, Some(&s.digest)).await?;
                        keep.extend(p.artifacts);
                    }
                }
                let mut removed = 0;
                let mut bytes = 0;
                let mut busy = 0;
                for item in std::fs::read_dir(self.store.join("artifacts"))? {
                    let p = item?.path();
                    if p.extension().and_then(|s| s.to_str()) != Some("db") {
                        continue;
                    }
                    let h = p
                        .file_stem()
                        .and_then(|s| s.to_str())
                        .context("artifact name")?;
                    storage::digest(h)?;
                    if keep.contains(h) {
                        continue;
                    }
                    let size = std::fs::metadata(&p)?.len();
                    match std::fs::remove_file(p) {
                        Ok(()) => {
                            removed += 1;
                            bytes += size;
                        }
                        Err(e) if artifact_is_busy(&e) => busy += 1,
                        Err(e) => return Err(e.into()),
                    }
                }
                Ok(
                    json!({"status":"pruned","removed_artifacts":removed,"removed_bytes":bytes,"busy_artifacts":busy,"kept_artifacts":keep.len()}),
                )
            }
            Request::Pause {} => {
                ensure!(self.active.is_none(), "pause requires idle view");
                self.parent = Parent::cold(&self.store, &self.config.base, None).await?;
                self.paused = true;
                Ok(json!({"status":"paused"}))
            }
            Request::Rebase { expected_commit } => {
                ensure!(self.active.is_none(), "rebase requires idle view");
                if self.tip.as_deref() != Some(&expected_commit) {
                    return Ok(json!({"status":"parent_changed"}));
                }
                let fresh = Parent::cold(&self.store, &self.config.base, None).await?;
                self.commit(None, None, "rebase")?;
                self.parent = fresh;
                self.paused = false;
                Ok(json!({"status":"rebased","commit":self.tip}))
            }
            Request::Shutdown {} => Ok(json!({"status":"shutdown"})),
        }
    }
}
fn emit(v: &Value) -> Result<()> {
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    serde_json::to_writer(&mut out, v)?;
    out.write_all(b"\n")?;
    out.flush()?;
    Ok(())
}
#[tokio::main]
async fn main() -> Result<()> {
    let args: Vec<_> = std::env::args_os().collect();
    ensure!(args.len() == 3, "usage: redpanda-sandbox STORE BASE");
    let mut service = Service::open(PathBuf::from(&args[1]), PathBuf::from(&args[2])).await?;
    emit(
        &json!({"status":"ready","version":3,"depth":service.parent.depth,"cold_hashed_bytes":service.parent.hashed_bytes}),
    )?;
    for line in std::io::stdin().lock().lines() {
        let r: Request = match serde_json::from_str(&line?) {
            Ok(r) => r,
            Err(e) => {
                emit(&json!({"status":"invalid_request","error":e.to_string()}))?;
                continue;
            }
        };
        let stop = matches!(r, Request::Shutdown {});
        let result = match service.request(r).await {
            Ok(value) => value,
            Err(error) => json!({"status":"error","error":format!("{error:#}")}),
        };
        emit(&result)?;
        if stop {
            break;
        }
    }
    if let Some(c) = service.active.take() {
        if let Some(mount) = c.mount {
            mount.unmount().await?;
        }
    }
    Ok(())
}
