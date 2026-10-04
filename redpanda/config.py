"""个人模型配置；连接凭据单独管理。"""
from __future__ import annotations
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from redpanda.llm.api import LLMApi
from redpanda.llm.config import ModelConfig
from redpanda.paths import RedPandaHome

CONFIG_PATH_ENV = "REDPANDA_CONFIG"
INITIAL_CONFIG = {"model": {"default": "deepseek/deepseek-v4-pro", "candidates": [
    {"model": "deepseek/deepseek-v4-pro", "compact_threshold_tokens": 200000},
]}}


@dataclass(frozen=True, slots=True)
class AppConfig:
    default_model: str | None
    models: tuple[ModelConfig, ...]

    def get_model(self, model: str) -> ModelConfig:
        for candidate in self.models:
            if candidate.model == model:
                return candidate
        raise ValueError(f"模型不在候选列表中：{model}")

    @property
    def default(self) -> ModelConfig | None:
        return None if self.default_model is None else self.get_model(self.default_model)

    def to_dict(self) -> dict:
        return {"model": {"default": self.default_model,
                          "candidates": [asdict(model) for model in self.models]}}


@dataclass(frozen=True, slots=True)
class AssistantConfig:
    model_name: str
    compact_threshold_tokens: int
    llm: LLMApi


def config_path() -> Path:
    return Path(os.environ[CONFIG_PATH_ENV]) if CONFIG_PATH_ENV in os.environ else RedPandaHome.default().config_path


def write_json(path: Path, data: dict) -> None:
    """完整文档以替换方式发布，读者只会看到完整快照。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
        except BaseException:
            file.close()
            temporary.unlink()
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def parse_app_config(data: object) -> AppConfig:
    if type(data) is not dict or set(data) != {"model"}:
        raise ValueError("配置字段必须只有 model")
    model = data["model"]
    if type(model) is not dict or set(model) != {"default", "candidates"}:
        raise ValueError("model 配置字段必须是 default/candidates")
    if type(model["candidates"]) is not list:
        raise ValueError("candidates 必须是数组")
    candidates = tuple(ModelConfig.from_dict(item) for item in model["candidates"])
    names = [item.model for item in candidates]
    if len(names) != len(set(names)):
        raise ValueError("候选模型不能重复")
    default = model["default"]
    if default is not None and (type(default) is not str or default not in names):
        raise ValueError("默认模型必须来自候选列表")
    if candidates and default is None:
        raise ValueError("有候选模型时必须选择默认模型")
    return AppConfig(default, candidates)


def load_app_config(path: Path | None = None) -> AppConfig:
    target = config_path() if path is None else path
    if not target.is_file():
        if path is not None or CONFIG_PATH_ENV in os.environ:
            raise FileNotFoundError(f"配置不存在：{target}")
        write_json(target, INITIAL_CONFIG)
    return parse_app_config(json.loads(target.read_text(encoding="utf-8")))


def assistant_config_from_app(app: AppConfig, llm: LLMApi) -> AssistantConfig:
    model = app.default
    return AssistantConfig("" if model is None else model.model,
                           200000 if model is None else model.compact_threshold_tokens, llm)
