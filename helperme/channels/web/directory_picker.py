"""本机 Web 入口的目录选择；GUI 在独立进程的主线程运行。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys


class DirectoryPickerUnavailable(Exception):
    """当前 Python 或桌面环境无法打开目录选择窗口。"""


async def select_directory() -> Path | None:
    command = (sys.executable, "-I", str(Path(__file__).resolve()))
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        stdout, stderr = await asyncio.to_thread(process.communicate)
    except asyncio.CancelledError:
        if process.poll() is None:
            process.terminate()
        await asyncio.to_thread(process.wait)
        raise

    if process.returncode == 2:
        raise DirectoryPickerUnavailable(stderr.decode("utf-8").strip())
    if process.returncode != 0:
        error = subprocess.CalledProcessError(
            process.returncode, command,
            output=stdout, stderr=stderr,
        )
        error.add_note(stderr.decode("utf-8"))
        raise error
    selected = json.loads(stdout)
    if selected is None:
        return None
    if type(selected) is not str or not selected or not Path(selected).is_absolute():
        raise ValueError("目录选择进程必须返回绝对路径或 null")
    return Path(selected)


def _main() -> None:
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        import tkinter
        from tkinter import filedialog
    except ModuleNotFoundError as error:
        if error.name not in {"tkinter", "_tkinter"}:
            raise
        print("当前 Python 未安装 Tcl/Tk，无法打开文件夹选择窗口。", file=sys.stderr)
        raise SystemExit(2) from error

    try:
        root = tkinter.Tk()
    except tkinter.TclError as error:
        print(f"无法连接本机图形桌面，无法打开文件夹选择窗口：{error}", file=sys.stderr)
        raise SystemExit(2) from error
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            parent=root,
            title="选择工作区文件夹",
            mustexist=True,
            initialdir=str(Path.home()),
        )
    finally:
        root.destroy()
    print(json.dumps(selected if selected else None))


if __name__ == "__main__":
    _main()
