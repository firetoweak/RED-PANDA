"""Application model selection and project-owned Ferro connection settings."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


FERRO_VERSION = "v1.5.8"
FERRO_BASE_URL = "http://127.0.0.1:18787/v1"
FERRO_MASTER_KEY_ENV = "FERRO_MASTER_KEY"
FERRO_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """The HelperMe-selected model name offered by Ferro."""

    active: str

    def __post_init__(self) -> None:
        if type(self.active) is not str or not self.active.strip():
            raise ValueError("model.active must be a non-empty str")
        object.__setattr__(self, "active", self.active.strip())


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """Connection details for the project-local Ferro HTTP service."""

    base_url: str
    api_key: str

    def __post_init__(self) -> None:
        if type(self.base_url) is not str or not self.base_url.strip():
            raise ValueError("gateway base_url must be a non-empty str")
        base_url = self.base_url.strip().rstrip("/")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.rstrip("/").endswith("/v1")
        ):
            raise ValueError("gateway base_url must be an HTTP(S) /v1 URL")
        if type(self.api_key) is not str or not self.api_key.strip():
            raise ValueError("FERRO_MASTER_KEY must be a non-empty str")
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "api_key", self.api_key.strip())


def load_gateway_config() -> GatewayConfig:
    """Load the project .env for the HTTP client; OS environment wins."""
    load_dotenv(FERRO_PROJECT_ROOT / ".env", override=False)
    api_key = os.environ.get(FERRO_MASTER_KEY_ENV)
    if api_key is None:
        raise ValueError(
            f"请在项目 .env 中设置 {FERRO_MASTER_KEY_ENV}，"
            "并确保 Ferro 使用同一个 Master Key"
        )
    return GatewayConfig(
        base_url=FERRO_BASE_URL,
        api_key=api_key,
    )
