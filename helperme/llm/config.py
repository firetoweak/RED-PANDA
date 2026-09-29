"""Application model selection and project .env provider settings."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv
from thinllm import PROVIDERS, Endpoint, MissingProviderSetting, resolve_endpoint


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """The HelperMe-selected model, written as provider/model."""

    active: str

    def __post_init__(self) -> None:
        if type(self.active) is not str:
            raise ValueError("model.active must be a str")
        active = self.active.strip()
        provider, _, model = active.partition("/")
        if provider not in PROVIDERS or not model:
            raise ValueError(
                "model.active 必须形如 <provider>/<model>，provider 为 "
                + "、".join(PROVIDERS)
            )
        object.__setattr__(self, "active", active)


def load_endpoint(model: ModelConfig) -> Endpoint:
    """Load the project .env for the selected provider; OS environment wins."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    try:
        return resolve_endpoint(model.active, os.environ)
    except MissingProviderSetting as missing:
        raise ValueError(f"请在项目 .env 中设置 {missing.variable}") from missing
