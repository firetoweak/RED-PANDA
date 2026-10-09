mod namespace;
use crate::{storage, Change};
use anyhow::{ensure, Context, Result};
pub use namespace::{apply, bindings, plan};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{collections::BTreeSet, fs::File, io::Read, path::Path};
use storage::Image;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Policy {
    Original,
    Preserve,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Operation {
    Execute,
    Restore {
        targets: Vec<String>,
        policy: Policy,
    },
}
impl Operation {
    pub fn validate(&self) -> Result<()> {
        if let Self::Restore { targets, .. } = self {
            ensure!(!targets.is_empty(), "empty restore range");
            let mut seen = BTreeSet::new();
            for id in targets {
                storage::identifier(id)?;
                ensure!(seen.insert(id), "duplicate restore target");
            }
        }
        Ok(())
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Completed {
    pub command_id: String,
    pub parent_commit: Option<String>,
}

pub struct Conflict {
    pub reason: &'static str,
    pub path: String,
}

fn validate(image: &Image) -> Result<()> {
    ensure!(
        image.chunk_size == vfs_core::config::DEFAULT_CHUNK_SIZE as u64,
        "invalid block geometry"
    );
    if image.kind == "missing" {
        ensure!(image.identity.is_none(), "missing object has identity");
    } else {
        ensure!(
            image.identity.as_ref().is_none_or(|id| !id.is_empty()),
            "empty object identity"
        );
    }
    match image.kind.as_str() {
        "file" => {
            ensure!(image.target.is_none(), "file image has a symlink target");
            for (&key, value) in &image.blocks {
                let offset = key
                    .checked_mul(image.chunk_size)
                    .context("block offset overflow")?;
                if offset < image.size {
                    storage::digest(value.as_deref().context("block evidence absent")?)?;
                } else {
                    ensure!(value.is_none(), "block beyond EOF");
                }
            }
            if image.complete {
                for key in 0..image.size.div_ceil(image.chunk_size) {
                    ensure!(image.blocks.contains_key(&key), "incomplete full image");
                }
            }
        }
        "symlink" => {
            let target = image.target.as_deref().context("symlink target absent")?;
            ensure!(
                image.blocks.is_empty() && image.complete && image.size == target.len() as u64,
                "invalid symlink image"
            );
        }
        "missing" | "directory" => ensure!(
            image.size == 0 && image.blocks.is_empty() && image.complete && image.target.is_none(),
            "invalid non-file image"
        ),
        _ => anyhow::bail!("invalid image kind"),
    }
    Ok(())
}

fn valid_path(path: &str) -> Result<()> {
    ensure!(path.starts_with('/'), "invalid view path");
    for part in path[1..].split('/') {
        ensure!(
            !part.is_empty() && part != "." && part != ".." && !part.contains(['\\', ':', '\0']),
            "invalid view path"
        );
    }
    Ok(())
}

fn block(cas: &Path, image: &Image, key: u64) -> Result<Vec<u8>> {
    let offset = key
        .checked_mul(image.chunk_size)
        .context("block offset overflow")?;
    if offset >= image.size {
        return Ok(vec![]);
    }
    let hash = image
        .blocks
        .get(&key)
        .and_then(Option::as_deref)
        .context("required block absent")?;
    storage::digest(hash)?;
    let length = (image.size - offset).min(image.chunk_size);
    let mut file = File::open(cas.join(hash))?;
    ensure!(
        file.metadata()?.len() == length,
        "CAS evidence length differs"
    );
    let mut bytes = vec![0; length as usize];
    file.read_exact(&mut bytes)?;
    ensure!(
        format!("{:x}", Sha256::digest(&bytes)) == hash,
        "CAS evidence digest differs"
    );
    Ok(bytes)
}

fn verify(cas: &Path, image: &Image) -> Result<()> {
    validate(image)?;
    if image.kind != "missing" {
        ensure!(
            image.identity.as_ref().is_some_and(|id| !id.is_empty()),
            "operation identity absent"
        );
    }
    for &key in image.blocks.keys() {
        block(cas, image, key)?;
    }
    Ok(())
}

fn choose(
    cas: &Path,
    change: &Change,
    current: &Image,
    policy: &Policy,
) -> Result<std::result::Result<Image, Conflict>> {
    let source = &change.after;
    let destination = &change.before;
    if current.size != source.size {
        return Ok(Err(Conflict {
            reason: "length_changed",
            path: change.path.clone(),
        }));
    }
    let keys: BTreeSet<_> = source
        .blocks
        .keys()
        .chain(destination.blocks.keys())
        .copied()
        .collect();
    // A human value in a tail to be removed protects the length and the entire result.
    if matches!(policy, Policy::Preserve) && destination.size < source.size {
        for &key in &keys {
            let after = block(cas, source, key)?;
            let data = block(cas, current, key)?;
            let start = destination.size.saturating_sub(key * source.chunk_size) as usize;
            if start < after.len() && after[start..] != data[start..] {
                return Ok(Ok(current.clone()));
            }
        }
    }
    let mut desired = current.clone();
    desired.size = destination.size;
    let mut preserved = false;
    for key in keys {
        let source_bytes = block(cas, source, key)?;
        let mut result = block(cas, destination, key)?;
        let data = block(cas, current, key)?;
        for index in 0..source_bytes.len().min(result.len()) {
            if source_bytes[index] == result[index]
                || (matches!(policy, Policy::Preserve) && data[index] != source_bytes[index])
            {
                if matches!(policy, Policy::Preserve)
                    && source_bytes[index] != result[index]
                    && data[index] != source_bytes[index]
                {
                    preserved = true;
                }
                result[index] = data[index];
            }
        }
        let hash = if result.is_empty() {
            None
        } else {
            Some(storage::save_block(cas, &result)?)
        };
        desired.blocks.insert(key, hash);
    }
    for (&key, hash) in &mut desired.blocks {
        if key * desired.chunk_size >= desired.size {
            *hash = None;
        }
    }
    if !preserved {
        desired.mode = destination.mode;
    }
    validate(&desired)?;
    Ok(Ok(desired))
}
