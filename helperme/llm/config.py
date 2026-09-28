"""LiteLLM Router 的应用侧配置。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelConfig:
    active: str
    router: dict[str, object]

    def __post_init__(self) -> None:
        if type(self.active) is not str or not self.active:
            raise ValueError("active must be a non-empty str")
        if type(self.router) is not dict or not self.router:
            raise ValueError("router must be a non-empty dict")
        object.__setattr__(self, "router", deepcopy(self.router))
