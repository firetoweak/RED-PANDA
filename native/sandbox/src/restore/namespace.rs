use super::{block, choose, valid_path, verify, Conflict, Policy};
use crate::{storage, tracking::TrackingFS, Change, Sealed};
use anyhow::{ensure, Context, Result};
use serde::Deserialize;
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
};
use storage::Image;
use vfs_core::FileSystem;

pub struct Entry {
    expected: Image,
    desired: Image,
}
pub struct Plan {
    paths: BTreeMap<String, Entry>,
    moves: Vec<(String, String)>,
}
#[derive(Default)]
struct Effect {
    before: Option<Image>,
    after: Option<Image>,
    before_names: BTreeSet<String>,
    after_names: BTreeSet<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct HostIndex {
    version: u32,
    active: Vec<String>,
    bindings: BTreeMap<String, String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Counts {
    changed_bytes: u64,
    preserved_bytes: u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PublishedStep {
    path: String,
    expected: Image,
    desired: Image,
    state: String,
    counts: Counts,
    created_identity: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Transaction {
    version: u32,
    kind: String,
    commands: Vec<String>,
    commit: String,
    steps: Vec<PublishedStep>,
    state: String,
    finalized: bool,
}

fn native_identity(native: &str) -> Result<()> {
    #[cfg(windows)]
    ensure!(
        native.len() == 48
            && native
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "invalid native binding"
    );
    #[cfg(unix)]
    {
        let mut parts = native.split(':');
        let (prefix, dev, ino, birth, rest) = (
            parts.next(),
            parts.next(),
            parts.next(),
            parts.next(),
            parts.next(),
        );
        let hex = |value: Option<&str>| {
            value.is_some_and(|text| {
                text.len() == 16
                    && text
                        .bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            })
        };
        ensure!(
            prefix == Some("unix") && hex(dev) && hex(ino) && hex(birth) && rest.is_none(),
            "invalid native binding"
        );
    }
    Ok(())
}
fn bind(result: &mut BTreeMap<String, String>, logical: &str, native: &str) -> Result<()> {
    native_identity(native)?;
    if logical != native {
        if let Some(previous) = result.insert(logical.into(), native.into()) {
            ensure!(
                previous == native,
                "logical object has conflicting native bindings"
            );
        }
    }
    Ok(())
}

fn flatten(mut bindings: BTreeMap<String, String>) -> Result<BTreeMap<String, String>> {
    let keys: Vec<_> = bindings.keys().cloned().collect();
    for key in keys {
        let mut next = key.clone();
        let mut seen = BTreeSet::new();
        while let Some(value) = bindings.get(&next) {
            ensure!(seen.insert(next.clone()), "cyclic native bindings");
            next = value.clone();
        }
        bindings.insert(key, next);
    }
    Ok(bindings)
}

// Completed creations establish identity lineage across copy/delete publication.
// The active index binds only the latest object at a path, never its predecessors.
pub fn bindings(store: &Path) -> Result<BTreeMap<String, String>> {
    let mut result = BTreeMap::new();
    let folder = store.join("host");
    if folder.exists() {
        for entry in std::fs::read_dir(folder)? {
            let path = entry?.path();
            if path.extension().is_none_or(|extension| extension != "json") {
                continue;
            }
            let transaction: Transaction = storage::read_json(&path)?;
            ensure!(
                transaction.version == 3
                    && matches!(transaction.kind.as_str(), "publish" | "undo")
                    && matches!(
                        transaction.state.as_str(),
                        "prepared" | "conflict" | "complete"
                    ),
                "invalid host transaction"
            );
            ensure!(
                !transaction.commands.is_empty()
                    && uuid::Uuid::parse_str(&transaction.commit)?.to_string()
                        == transaction.commit,
                "invalid transaction identity"
            );
            let mut seen = BTreeSet::new();
            for command in &transaction.commands {
                storage::identifier(command)?;
                ensure!(seen.insert(command), "duplicate transaction command");
            }
            for step in &transaction.steps {
                valid_path(&step.path)?;
                super::validate(&step.expected)?;
                super::validate(&step.desired)?;
                ensure!(
                    step.expected.kind == "missing" || step.expected.identity.is_some(),
                    "host observation identity absent"
                );
                ensure!(
                    matches!(step.state.as_str(), "prepared" | "applying" | "done"),
                    "invalid step state"
                );
                if let Some(native) = &step.created_identity {
                    ensure!(
                        step.state != "prepared"
                            && step.expected.kind == "missing"
                            && matches!(
                                step.desired.kind.as_str(),
                                "file" | "directory" | "symlink"
                            ),
                        "invalid created identity state"
                    );
                    native_identity(native)?;
                }
                let _ = (step.counts.changed_bytes, step.counts.preserved_bytes);
                if transaction.finalized {
                    ensure!(
                        transaction.state == "complete" && step.state == "done",
                        "invalid finalized transaction"
                    );
                    if let (Some(native), Some(logical)) =
                        (&step.created_identity, &step.desired.identity)
                    {
                        bind(&mut result, logical, native)?;
                    }
                }
            }
        }
    }
    let path = store.join("host-index.json");
    if !path.exists() {
        return flatten(result);
    }
    let index: HostIndex = storage::read_json(&path)?;
    ensure!(index.version == 3, "invalid host index version");
    let mut seen = BTreeSet::new();
    let mut latest = BTreeMap::new();
    for id in index.active {
        storage::identifier(&id)?;
        ensure!(seen.insert(id.clone()), "duplicate active operation");
        let receipt: Sealed =
            storage::read_json(&store.join("commands").join(&id).join("sealed.json"))?;
        ensure!(
            receipt.version == 3 && receipt.command_id == id,
            "invalid published receipt"
        );
        receipt.operation.validate()?;
        for change in receipt.changes {
            latest.insert(change.path, change.after);
        }
    }
    for (path, native) in index.bindings {
        valid_path(&path)?;
        native_identity(&native)?;
        if let Some(image) = latest.get(&path) {
            if let Some(logical) = &image.identity {
                bind(&mut result, logical, &native)?;
            }
        }
    }
    flatten(result)
}

fn normalize(image: &Image, bindings: &BTreeMap<String, String>) -> Image {
    let mut image = image.clone();
    if let Some(native) = image.identity.as_ref().and_then(|id| bindings.get(id)) {
        image.identity = Some(native.clone());
    }
    image
}
fn merge(target: &mut Option<Image>, image: &Image) -> Result<()> {
    if let Some(value) = target {
        ensure!(
            value.kind == image.kind
                && value.size == image.size
                && value.identity == image.identity
                && value.chunk_size == image.chunk_size
                && value.mode == image.mode
                && value.target == image.target,
            "alias metadata differs"
        );
        for (&key, hash) in &image.blocks {
            if let Some(previous) = value.blocks.insert(key, hash.clone()) {
                ensure!(previous == *hash, "alias evidence differs");
            }
        }
        value.complete |= image.complete;
    } else {
        *target = Some(image.clone());
    }
    Ok(())
}
fn same_content(a: &Image, b: &Image) -> bool {
    a.kind == b.kind
        && a.size == b.size
        && a.mode == b.mode
        && a.target == b.target
        && a.blocks
            .iter()
            .filter(|(_, v)| v.is_some())
            .eq(b.blocks.iter().filter(|(_, v)| v.is_some()))
}
fn conflict(reason: &'static str, path: &str) -> Conflict {
    Conflict {
        reason,
        path: path.into(),
    }
}

fn reverse(
    paths: &mut BTreeMap<String, Entry>,
    changes: &[Change],
    cas: &Path,
    policy: &Policy,
) -> Result<std::result::Result<(), Conflict>> {
    let mut effects: BTreeMap<String, Effect> = BTreeMap::new();
    let mut blocked = BTreeSet::new();
    for change in changes {
        let current = &paths[&change.path].desired;
        let after = &change.after;
        if current.kind != after.kind {
            if matches!(policy, Policy::Preserve) {
                blocked.insert(change.path.clone());
            } else if current.kind != "missing" || after.kind == "missing" {
                return Ok(Err(conflict("structure_changed", &change.path)));
            }
        } else if after.kind != "missing" && current.identity != after.identity {
            return Ok(Err(conflict("identity_changed", &change.path)));
        }
        for (image, before) in [(&change.before, true), (&change.after, false)] {
            if let Some(identity) = &image.identity {
                let effect = effects.entry(identity.clone()).or_default();
                if before {
                    merge(&mut effect.before, image)?;
                    effect.before_names.insert(change.path.clone());
                } else {
                    merge(&mut effect.after, image)?;
                    effect.after_names.insert(change.path.clone());
                }
            }
        }
    }
    for effect in effects.values() {
        if effect.before.is_none()
            && effect.after.as_ref().is_some_and(|v| v.kind == "file")
            && matches!(policy, Policy::Preserve)
        {
            let path = effect.after_names.first().expect("object has no name");
            if !same_content(&paths[path].desired, effect.after.as_ref().unwrap()) {
                blocked.extend(effect.after_names.iter().cloned());
            }
        }
    }
    loop {
        let count = blocked.len();
        for effect in effects.values() {
            if effect
                .before_names
                .iter()
                .chain(&effect.after_names)
                .any(|p| blocked.contains(p))
            {
                blocked.extend(
                    effect
                        .before_names
                        .iter()
                        .chain(&effect.after_names)
                        .cloned(),
                );
            }
        }
        if count == blocked.len() {
            break;
        }
    }
    let mut removals = BTreeSet::new();
    let mut values = BTreeMap::new();
    let mut contents = BTreeMap::new();
    for (identity, effect) in effects {
        if effect
            .before_names
            .iter()
            .chain(&effect.after_names)
            .any(|p| blocked.contains(p))
        {
            continue;
        }
        let desired = match (&effect.before, &effect.after) {
            (Some(before), Some(after)) if before.kind == "file" => {
                let path = effect.after_names.first().expect("file has no name");
                let current = &paths[path].desired;
                if current.kind == "missing" {
                    if !before.complete {
                        return Ok(Err(conflict("content_unavailable", path)));
                    }
                    before.clone()
                } else {
                    let change = Change {
                        path: path.clone(),
                        before: before.clone(),
                        after: after.clone(),
                    };
                    match choose(cas, &change, current, policy)? {
                        Ok(value) => value,
                        Err(error) => return Ok(Err(error)),
                    }
                }
            }
            (Some(before), Some(_)) => {
                if effect.before_names != effect.after_names {
                    return Ok(Err(conflict(
                        "unsupported_directory_rename",
                        effect.before_names.first().unwrap(),
                    )));
                }
                before.clone()
            }
            (Some(before), None) => {
                ensure!(before.complete, "structural preimage is incomplete");
                before.clone()
            }
            (None, Some(_)) => Image::missing(),
            (None, None) => unreachable!("effect has no images"),
        };
        if desired.kind == "file" {
            contents.insert(identity, desired.clone());
        }
        removals.extend(effect.after_names);
        for path in effect.before_names {
            values.insert(path, desired.clone());
        }
    }
    for path in removals {
        paths.get_mut(&path).unwrap().desired = Image::missing();
    }
    for (path, image) in values {
        paths.get_mut(&path).unwrap().desired = image;
    }
    // Content changes affect every known alias still bound to this object.
    for entry in paths.values_mut() {
        if let Some(image) = entry
            .desired
            .identity
            .as_ref()
            .and_then(|id| contents.get(id))
        {
            entry.desired = image.clone();
        }
    }
    Ok(Ok(()))
}

pub async fn plan(
    fs: &dyn FileSystem,
    cas: &Path,
    receipts: &[Sealed],
    policy: &Policy,
    bindings: &BTreeMap<String, String>,
) -> Result<std::result::Result<Plan, Conflict>> {
    let mut ranges: BTreeMap<String, BTreeSet<u64>> = BTreeMap::new();
    let mut full = BTreeSet::new();
    let mut names = BTreeSet::new();
    let mut normalized = Vec::new();
    for receipt in receipts {
        ensure!(receipt.version == 3, "invalid sealed version");
        receipt.operation.validate()?;
        let mut changes = Vec::new();
        for change in &receipt.changes {
            valid_path(&change.path)?;
            verify(cas, &change.before)?;
            verify(cas, &change.after)?;
            let change = Change {
                path: change.path.clone(),
                before: normalize(&change.before, bindings),
                after: normalize(&change.after, bindings),
            };
            names.insert(change.path.clone());
            for image in [&change.before, &change.after] {
                if let Some(id) = &image.identity {
                    ranges
                        .entry(id.clone())
                        .or_default()
                        .extend(image.blocks.keys().copied());
                    if image.complete {
                        full.insert(id.clone());
                    }
                }
            }
            changes.push(change);
        }
        normalized.push(changes);
    }
    let mut paths = BTreeMap::new();
    for path in names {
        let mut expected =
            storage::metadata(fs, &path, vfs_core::config::DEFAULT_CHUNK_SIZE as u64).await?;
        if let Some(identity) = expected.identity.clone() {
            let complete = full.contains(&identity);
            let keys = if complete {
                (0..expected.size.div_ceil(expected.chunk_size)).collect()
            } else {
                ranges.get(&identity).cloned().unwrap_or_default()
            };
            storage::capture_blocks(fs, &path, cas, &mut expected, keys.into_iter()).await?;
            expected.complete = complete || expected.kind != "file";
        }
        paths.insert(
            path,
            Entry {
                desired: expected.clone(),
                expected,
            },
        );
    }
    for changes in normalized.iter().rev() {
        if let Err(error) = reverse(&mut paths, changes, cas, policy)? {
            return Ok(Err(error));
        }
    }
    let mut directories: Vec<_> = paths
        .iter()
        .filter(|(_, e)| e.expected.kind == "directory" && e.desired.kind == "missing")
        .map(|(p, _)| p.clone())
        .collect();
    directories.sort_by_key(|p| std::cmp::Reverse(p.matches('/').count()));
    for path in directories {
        let ino = storage::resolve(fs, &path)
            .await?
            .context("directory absent")?;
        for name in fs.readdir(ino).await?.context("directory absent")? {
            let child = format!("{path}/{name}");
            if paths
                .get(&child)
                .is_none_or(|entry| entry.desired.kind != "missing")
            {
                if matches!(policy, Policy::Preserve) {
                    let entry = paths.get_mut(&path).unwrap();
                    entry.desired = entry.expected.clone();
                    break;
                }
                return Ok(Err(conflict("directory_contains_user_children", &path)));
            }
        }
    }
    for (path, entry) in &paths {
        if entry.expected.kind == "directory"
            && entry.desired.kind == "directory"
            && entry.expected.identity != entry.desired.identity
        {
            return Ok(Err(conflict("unsupported_directory_replacement", path)));
        }
        if entry.desired.kind != "missing" {
            let parent = path.rsplit_once('/').unwrap().0;
            let kind = match paths.get(parent) {
                Some(entry) => entry.desired.kind.clone(),
                None => {
                    storage::metadata(fs, parent, entry.desired.chunk_size)
                        .await?
                        .kind
                }
            };
            if kind != "directory" {
                return Ok(Err(conflict("parent_changed", path)));
            }
        }
    }
    let mut moves = BTreeMap::new();
    let mut created = BTreeSet::new();
    for (path, entry) in &paths {
        if entry.desired.kind != "file"
            || (entry.expected.kind == "file" && entry.expected.identity == entry.desired.identity)
        {
            continue;
        }
        let source = paths.iter().find(|(other, e)| {
            *other != path
                && e.expected.kind == "file"
                && e.expected.identity == entry.desired.identity
                && e.desired.identity != e.expected.identity
        });
        if let Some((source, _)) = source {
            if entry.expected.kind == "directory"
                || moves.insert(source.clone(), path.clone()).is_some()
            {
                return Ok(Err(conflict("unsupported_link_restore", path)));
            }
        } else {
            let identity = entry
                .desired
                .identity
                .as_ref()
                .context("desired identity absent")?;
            if !created.insert(identity.clone())
                || paths.values().any(|e| {
                    e.expected.kind == "file" && e.expected.identity.as_ref() == Some(identity)
                })
            {
                return Ok(Err(conflict("unsupported_link_restore", path)));
            }
        }
    }
    let mut ordered = Vec::new();
    while !moves.is_empty() {
        let next = moves
            .iter()
            .find(|(_, to)| !moves.contains_key(*to))
            .map(|(from, to)| (from.clone(), to.clone()));
        let Some((from, to)) = next else {
            return Ok(Err(conflict(
                "unsupported_rename_cycle",
                moves.first_key_value().unwrap().0,
            )));
        };
        moves.remove(&from);
        ordered.push((from, to));
    }
    Ok(Ok(Plan {
        paths,
        moves: ordered,
    }))
}

async fn location(fs: &dyn FileSystem, path: &str) -> Result<(i64, String)> {
    let (parent, name) = path.rsplit_once('/').context("invalid path")?;
    Ok((
        storage::resolve(fs, parent)
            .await?
            .context("parent absent")?,
        name.into(),
    ))
}
pub async fn apply(fs: &TrackingFS, cas: &Path, plan: &Plan) -> Result<()> {
    for (path, entry) in &plan.paths {
        let mut current = storage::metadata(fs, path, entry.expected.chunk_size).await?;
        storage::capture_blocks(
            fs,
            path,
            cas,
            &mut current,
            entry.expected.blocks.keys().copied(),
        )
        .await?;
        current.complete = entry.expected.complete;
        ensure!(
            current == entry.expected,
            "file changed after restore preparation"
        );
        if entry.expected.kind != entry.desired.kind
            || entry.expected.identity != entry.desired.identity
        {
            fs.capture(path, None).await?;
        }
    }
    let moving: BTreeSet<_> = plan.moves.iter().map(|(from, _)| from).collect();
    for (path, entry) in &plan.paths {
        let remove_file = entry.expected.kind == "file"
            && (entry.desired.kind != "file" || entry.expected.identity != entry.desired.identity)
            && !moving.contains(path);
        let remove_link = entry.expected.kind == "symlink"
            && (entry.desired.kind != "symlink"
                || entry.expected.target != entry.desired.target
                || entry.expected.mode != entry.desired.mode);
        if remove_file || remove_link {
            let (parent, name) = location(fs, path).await?;
            fs.unlink(parent, &name).await?;
        }
    }
    let mut directories: Vec<_> = plan
        .paths
        .iter()
        .filter(|(_, e)| e.desired.kind == "directory" && e.expected.kind != "directory")
        .map(|(p, _)| p)
        .collect();
    directories.sort_by_key(|p| p.matches('/').count());
    for path in directories {
        let (parent, name) = location(fs, path).await?;
        fs.mkdir(parent, &name, 0o755, 0, 0).await?;
    }
    for (from, to) in &plan.moves {
        let (parent, name) = location(fs, from).await?;
        let (target, new_name) = location(fs, to).await?;
        fs.rename(parent, &name, target, &new_name).await?;
    }
    let mut removed: Vec<_> = plan
        .paths
        .iter()
        .filter(|(_, e)| e.expected.kind == "directory" && e.desired.kind != "directory")
        .map(|(p, _)| p)
        .collect();
    removed.sort_by_key(|p| std::cmp::Reverse(p.matches('/').count()));
    for path in removed {
        let (parent, name) = location(fs, path).await?;
        fs.rmdir(parent, &name).await?;
    }
    let mut written = BTreeSet::new();
    for (path, entry) in &plan.paths {
        if entry.desired.kind != "file" {
            continue;
        }
        let identity = entry
            .desired
            .identity
            .as_ref()
            .context("desired file identity absent")?;
        if !written.insert(identity) {
            continue;
        }
        let existing = storage::resolve(fs, path).await?;
        let file = if let Some(ino) = existing {
            fs.open(ino, libc::O_RDWR).await?
        } else {
            ensure!(entry.desired.complete, "new file image incomplete");
            let (parent, name) = location(fs, path).await?;
            fs.create_file(parent, &name, 0o644, 0, 0).await?.1
        };
        for &key in entry.desired.blocks.keys() {
            let data = block(cas, &entry.desired, key)?;
            if data.is_empty() {
                continue;
            }
            let offset = key * entry.desired.chunk_size;
            if existing.is_some() && file.pread(offset, data.len() as u64).await? == data {
                continue;
            }
            for (alias, other) in &plan.paths {
                if other.desired.kind == "file" && other.desired.identity.as_ref() == Some(identity)
                {
                    fs.capture(
                        alias,
                        Some((offset.min(entry.expected.size), offset + data.len() as u64)),
                    )
                    .await?;
                }
            }
            file.pwrite(offset, &data).await?;
        }
        let old_size = file.fstat().await?.size as u64;
        if old_size != entry.desired.size {
            for (alias, other) in &plan.paths {
                if other.desired.kind == "file" && other.desired.identity.as_ref() == Some(identity)
                {
                    fs.capture(
                        alias,
                        Some((
                            old_size.min(entry.desired.size),
                            old_size.max(entry.desired.size),
                        )),
                    )
                    .await?;
                }
            }
            file.truncate(entry.desired.size).await?;
        }
        file.fsync().await?;
        if let Some(mode) = entry.desired.mode {
            let ino = storage::resolve(fs, path)
                .await?
                .context("file absent after write")?;
            fs.chmod(ino, mode).await?;
        }
    }
    for (path, entry) in &plan.paths {
        if entry.desired.kind != "symlink" {
            continue;
        }
        if entry.expected.kind == "symlink"
            && entry.expected.target == entry.desired.target
            && entry.expected.mode == entry.desired.mode
        {
            continue;
        }
        let target = entry
            .desired
            .target
            .as_deref()
            .context("symlink target absent")?;
        let (parent, name) = location(fs, path).await?;
        fs.symlink(parent, &name, target, 0, 0).await?;
    }
    fs.finalize().await?;
    Ok(())
}
