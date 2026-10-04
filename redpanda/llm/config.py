"""模型标识与个人连接文件的外部边界。"""
from __future__ import annotations
from dataclasses import dataclass
import json
from pathlib import Path
from urllib.parse import urlsplit

from thinllm import PROVIDERS, Endpoint, MissingProviderSetting, resolve_endpoint
from redpanda.llm.api import LLMAuthenticationError
from redpanda.paths import RedPandaHome


@dataclass(frozen=True, slots=True)
class ModelConfig:
    model: str
    compact_threshold_tokens: int

    def __post_init__(self):
        if type(self.model) is not str:
            raise ValueError("model 必须是字符串")
        provider, _, name = self.model.partition("/")
        if self.model != self.model.strip() or provider not in PROVIDERS or not name.strip():
            raise ValueError("model 必须形如 <provider>/<model>，provider 为 " + "、".join(PROVIDERS))
        if type(self.compact_threshold_tokens) is not int or self.compact_threshold_tokens <= 0:
            raise ValueError("compact_threshold_tokens 必须是大于 0 的整数")

    @classmethod
    def from_dict(cls, data: object) -> "ModelConfig":
        if type(data) is not dict or set(data) != {"model", "compact_threshold_tokens"}:
            raise ValueError("候选模型字段必须是 model/compact_threshold_tokens")
        return cls(**data)


def initial_connections() -> dict:
    defaults = {"vllm": "http://127.0.0.1:8000/v1", "ollama": "http://127.0.0.1:11434/v1"}
    return {name: ({"api_key": ""} if provider.base_url is not None else
                   {"api_key": "", "base_url": defaults[name]})
            for name, provider in PROVIDERS.items()}


def load_connections(path: Path) -> dict[str, dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if type(data) is not dict or set(data) != set(PROVIDERS):
        raise ValueError("connections.json 必须包含全部内置供应商")
    for name, provider in PROVIDERS.items():
        connection = data[name]
        fields = {"api_key"} if provider.base_url is not None else {"api_key", "base_url"}
        if type(connection) is not dict or set(connection) != fields:
            raise ValueError(f"{name} 连接字段必须是 {'/'.join(sorted(fields))}")
        if any(type(value) is not str for value in connection.values()):
            raise ValueError(f"{name} 连接设置必须是字符串")
        if provider.base_url is None and connection["base_url"].strip():
            address = urlsplit(connection["base_url"].strip())
            if address.scheme not in ("http", "https") or not address.hostname or address.query or address.fragment:
                raise ValueError(f"{name} base_url 必须是 HTTP(S) 服务地址")
    return data


def load_endpoint(model: ModelConfig, path: Path | None = None) -> Endpoint:
    name = model.model.partition("/")[0]
    connections = load_connections(RedPandaHome.default().connections_path if path is None else path)
    try:
        return resolve_endpoint(model.model, connections[name])
    except MissingProviderSetting as error:
        raise LLMAuthenticationError(f"请在 connections.json 中设置 {name}.{error.setting}") from error
