from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from redpanda.skills.models import (
    SkillBundle,
    SkillFile,
    SkillPackageLimits,
    SkillSourceRef,
    validate_skill_id,
    validate_skill_description,
)


class SkillPackageError(ValueError):
    """外部 Skill 包不符合安装契约。"""


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: yaml.SafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise SkillPackageError(f"Frontmatter 字段重复: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def validate_relative_skill_path(relative_path: str) -> str:
    if not isinstance(relative_path, str) or not relative_path:
        raise SkillPackageError("Skill 包路径不能为空")
    if "\\" in relative_path:
        raise SkillPackageError("Skill 包路径必须使用 / 分隔")
    candidate = PurePosixPath(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise SkillPackageError(f"Skill 包路径非法: {relative_path}")
    if any(part in {"", "."} for part in candidate.parts):
        raise SkillPackageError(f"Skill 包路径非法: {relative_path}")
    normalized = candidate.as_posix()
    if normalized != relative_path:
        raise SkillPackageError(f"Skill 包路径未规范化: {relative_path}")
    return normalized


def parse_skill_markdown(content: bytes) -> tuple[str, str, str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillPackageError("SKILL.md 必须是 UTF-8") from exc
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise SkillPackageError("SKILL.md 必须以 YAML Frontmatter 开头")
    closing = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.rstrip("\r\n") == "---"
        ),
        None,
    )
    if closing is None:
        raise SkillPackageError("SKILL.md Frontmatter 缺少结束分隔符")
    raw_frontmatter = "".join(lines[1:closing])
    try:
        metadata = yaml.load(raw_frontmatter, Loader=_UniqueKeyLoader)
    except SkillPackageError:
        raise
    except yaml.YAMLError as exc:
        raise SkillPackageError("SKILL.md Frontmatter YAML 无效") from exc
    if not isinstance(metadata, dict):
        raise SkillPackageError("SKILL.md Frontmatter 必须是 object")
    name = metadata.get("name")
    description = metadata.get("description")
    if not isinstance(name, str):
        raise SkillPackageError("SKILL.md Frontmatter name 必须是 string")
    if not isinstance(description, str):
        raise SkillPackageError(
            "SKILL.md Frontmatter description 必须是非空 string"
        )
    description = description.strip()
    try:
        validate_skill_id(name)
        validate_skill_description(description)
    except ValueError as exc:
        raise SkillPackageError(str(exc)) from exc
    body = "".join(lines[closing + 1:])
    return name, description, body


def content_hash(files: tuple[SkillFile, ...]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda value: value.relative_path):
        path_bytes = item.relative_path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(item.content).to_bytes(8, "big"))
        digest.update(item.content)
    return digest.hexdigest()


def write_skill_bundle(target: Path, bundle: SkillBundle) -> None:
    target.mkdir(parents=True, exist_ok=False)
    for item in bundle.files:
        relative_path = validate_relative_skill_path(item.relative_path)
        destination = target.joinpath(*PurePosixPath(relative_path).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(item.content)


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


class LocalSkillPackageReader:
    def __init__(self, limits: SkillPackageLimits | None = None) -> None:
        self.limits = SkillPackageLimits() if limits is None else limits

    def read(self, source_directory: Path) -> SkillBundle:
        source_directory = source_directory.absolute()
        if _is_link(source_directory):
            raise SkillPackageError("Skill 源目录不能是 symlink 或 junction")
        source_directory = source_directory.resolve()
        if not source_directory.is_dir():
            raise SkillPackageError(f"Skill 源不是目录: {source_directory}")

        files: list[SkillFile] = []
        total_bytes = 0
        for root, directory_names, file_names in os.walk(
            source_directory,
            topdown=True,
            followlinks=False,
        ):
            root_path = Path(root)
            directory_names.sort()
            file_names.sort()
            for directory_name in directory_names:
                directory = root_path / directory_name
                if _is_link(directory):
                    raise SkillPackageError(
                        f"Skill 包不允许 symlink 或 junction: "
                        f"{directory.relative_to(source_directory).as_posix()}"
                    )
            for file_name in file_names:
                path = root_path / file_name
                relative_path = validate_relative_skill_path(
                    path.relative_to(source_directory).as_posix()
                )
                if _is_link(path):
                    raise SkillPackageError(
                        f"Skill 包不允许 symlink 或 junction: {relative_path}"
                    )
                if not path.is_file():
                    raise SkillPackageError(
                        f"Skill 包只允许普通文件: {relative_path}"
                    )
                size = path.stat().st_size
                if size > self.limits.max_file_bytes:
                    raise SkillPackageError(
                        f"Skill 文件超出大小限制: {relative_path}"
                    )
                content = path.read_bytes()
                if len(content) > self.limits.max_file_bytes:
                    raise SkillPackageError(
                        f"Skill 文件超出大小限制: {relative_path}"
                    )
                total_bytes += len(content)
                if total_bytes > self.limits.max_total_bytes:
                    raise SkillPackageError("Skill 包超出总大小限制")
                files.append(SkillFile(relative_path, content))
                if len(files) > self.limits.max_files:
                    raise SkillPackageError("Skill 包超出文件数量限制")

        by_path = {item.relative_path: item for item in files}
        skill_markdown = by_path.get("SKILL.md")
        if skill_markdown is None:
            raise SkillPackageError("Skill 包缺少 SKILL.md")
        name, description, main_instructions = parse_skill_markdown(
            skill_markdown.content
        )
        if len(main_instructions) > self.limits.max_main_instruction_chars:
            raise SkillPackageError("Skill 主指令超出完整加载限制")
        frozen_files = tuple(sorted(files, key=lambda item: item.relative_path))
        return SkillBundle(
            name=name,
            description=description,
            main_instructions=main_instructions,
            source=SkillSourceRef("local", str(source_directory)),
            resolved_ref=source_directory.as_uri(),
            content_hash=content_hash(frozen_files),
            files=frozen_files,
        )
