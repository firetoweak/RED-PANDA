from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Mapping

from redpanda.paths import RedPandaHome
from redpanda.mcp.models import validate_server_id


class McpSecretStore:
    """按 server_id 隔离的本地 SecretStore。"""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @classmethod
    def from_home(cls, home: RedPandaHome) -> "McpSecretStore":
        return cls(home.mcp_root / "secrets")

    def put_namespace(
        self,
        server_id: str,
        values: Mapping[str, str],
    ) -> dict[str, str]:
        """写入整命名空间，返回逻辑名 → secret_ref 映射。"""
        validate_server_id(server_id)
        if any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in values.items()
        ):
            raise ValueError("secret values 必须是 string:string mapping")
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path_for(server_id)
        temporary = path.with_suffix(".tmp")
        payload = {"version": 1, "values": dict(values)}
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._restrict_file(temporary)
        os.replace(temporary, path)
        self._restrict_file(path)
        return {name: self.ref_for(server_id, name) for name in values}

    def resolve(self, secret_ref: str) -> str:
        server_id, name = self._parse_ref(secret_ref)
        values = self._read_namespace(server_id)
        if name not in values:
            raise KeyError(secret_ref)
        return values[name]

    def resolve_many(self, refs: Mapping[str, str]) -> dict[str, str]:
        return {
            key: self.resolve(secret_ref)
            for key, secret_ref in refs.items()
        }

    def snapshot_namespace(self, server_id: str) -> dict[str, str]:
        """返回命名空间快照，供跨 Registry 更新失败时恢复。"""
        return self._read_namespace(server_id)

    def delete_namespace(self, server_id: str) -> None:
        path = self._path_for(server_id)
        if path.exists():
            path.unlink()

    def _read_namespace(self, server_id: str) -> dict[str, str]:
        path = self._path_for(server_id)
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "values"}
            or type(payload["version"]) is not int
            or payload["version"] != 1
        ):
            raise ValueError(f"secret namespace 无效: {server_id}")
        values = payload["values"]
        if not isinstance(values, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in values.items()
        ):
            raise ValueError(f"secret namespace values 无效: {server_id}")
        return dict(values)

    def _path_for(self, server_id: str) -> Path:
        validate_server_id(server_id)
        return self._root / f"{server_id}.json"

    @staticmethod
    def ref_for(server_id: str, name: str) -> str:
        return f"mcp:{server_id}:{name}"

    @staticmethod
    def _parse_ref(secret_ref: str) -> tuple[str, str]:
        parts = secret_ref.split(":", 2)
        if len(parts) != 3 or parts[0] != "mcp" or not parts[1] or not parts[2]:
            raise ValueError(f"无效 secret_ref: {secret_ref}")
        validate_server_id(parts[1])
        return parts[1], parts[2]

    @staticmethod
    def _restrict_file(path: Path) -> None:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
