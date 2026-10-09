use std::path::PathBuf;

use crate::config::CoreConfig;
use crate::error::{Error, Result};

/// Storage location and typed filesystem configuration.
#[derive(Debug, Clone, Default)]
pub struct VfsOptions {
    pub(crate) path: Option<String>,
    pub core_config: Option<CoreConfig>,
}

impl VfsOptions {
    pub fn db_path(&self) -> Result<String> {
        let Some(path) = self.path.as_deref().filter(|path| *path != ":memory:") else {
            return Ok(":memory:".to_string());
        };
        let path = std::path::absolute(path)?;
        path.into_os_string()
            .into_string()
            .map_err(|path| Error::InvalidUtf8Path(PathBuf::from(path).display().to_string()))
    }

    pub fn ephemeral() -> Self {
        Self::default()
    }

    pub fn with_path(path: impl Into<String>) -> Self {
        Self {
            path: Some(path.into()),
            core_config: None,
        }
    }

    pub fn with_core_config(mut self, core_config: CoreConfig) -> Self {
        self.core_config = Some(core_config);
        self
    }
}
