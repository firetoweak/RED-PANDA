//! Resolve names at the overlay boundary; delta retains original entry spellings.
use super::*;
use crate::fs::FsError;

impl OverlayFS {
    pub(super) async fn resolve_name(&self, parent: i64, name: &str) -> Result<String> {
        Ok(self.resolve_lookup_name(parent, name).await?.0)
    }

    pub(super) async fn resolve_lookup_name(
        &self,
        parent: i64,
        name: &str,
    ) -> Result<(String, Option<Stats>)> {
        let info = self.get_inode_info(parent).ok_or(FsError::NotFound)?;
        if let Some(delta_parent) = self.resolve_delta_parent(&info).await? {
            // Exact spellings retain the existing fast path. Alias matching only
            // inspects this private directory, never the entire host workspace.
            if self.delta.lookup(delta_parent, name).await?.is_some() {
                return Ok((name.to_owned(), None));
            }
            let entries = self
                .delta
                .readdir(delta_parent)
                .await?
                .ok_or(FsError::NotADirectory)?;
            let mut matched: Option<String> = None;
            for entry in entries {
                if self.base.names_equal(&entry, name) {
                    if matched.is_some() {
                        return Err(FsError::Corrupt("equivalent delta names".into()).into());
                    }
                    matched = Some(entry);
                }
            }
            if let Some(entry) = matched {
                return Ok((entry, None));
            }
        }
        let path = self.build_path(parent, name)?;
        if self.is_whiteout(&path) {
            return Ok((name.to_owned(), None));
        }
        // A delta directory may replace a host file at the same path. The host
        // object is then not a directory; looking up a child in it is ENOTDIR
        // on Linux (openat) and must not fail the overlay operation.
        let base_parent = if info.layer == Layer::Base {
            self.base.getattr(info.underlying_ino).await?
        } else {
            self.resolve_base_path(&info.path).await?
        };
        if let Some(base_parent) = base_parent.filter(|stats| stats.is_directory()) {
            if let Some(entry) = self.base.lookup_named(base_parent.ino, name).await? {
                return Ok((entry.name, Some(entry.stats)));
            }
        }
        Ok((name.to_owned(), None))
    }
}
