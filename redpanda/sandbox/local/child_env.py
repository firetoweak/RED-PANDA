"""产品命令环境的 PATH 刷新与 Shell 输出约定。

Worker 是常驻进程，`os.environ` 是启动时的快照。安装器（winget 等）修改
用户/系统 PATH 后，运行中的 Worker 看不到变化。因此 PATH 在每次 spawn 前
从注册表重新合成。工作区命令保留独立的启动环境，不使用此 PATH 刷新。

已知限制：只刷新 PATH；安装器新设的其他变量（如 `NVM_HOME`）仍需重启
Worker 才可见。Unix 上包管理器一般装进已在 PATH 的目录，无需刷新。
"""

from __future__ import annotations

import os

# 禁用颜色、分页与交互行为，让 CLI 输出对模型友好。
CHILD_ENV_OVERLAY: dict[str, str] = {
    "NO_COLOR": "1",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "GH_PAGER": "cat",
    "CI": "1",
}


def latest_persistent_path() -> str | None:
    """读取最新持久化 PATH（Machine + User 拼接）；非 Windows 返回 None。"""
    if os.name != "nt":
        return None
    import winreg

    machine = _read_registry_path(
        winreg.HKEY_LOCAL_MACHINE,
        r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
    )
    user = _read_registry_path(winreg.HKEY_CURRENT_USER, r"Environment")
    parts = [value for value in (machine, user) if value]
    return ";".join(parts) if parts else None


def _read_registry_path(root: int, subkey: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(root, subkey) as key:
            value, value_type = winreg.QueryValueEx(key, "Path")
    except OSError:
        return None
    if value_type not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
        return None
    if value_type == winreg.REG_EXPAND_SZ:
        value = os.path.expandvars(value)
    return value
