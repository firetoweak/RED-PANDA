from __future__ import annotations

import asyncio
from contextlib import closing
import os
import json
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory


class VersionBackendError(OSError):
    """已识别的 Git 执行环境错误。"""


def _native_path(path: Path) -> Path:
    resolved = path.resolve()
    if os.name == "nt" and not str(resolved).startswith("\\\\?\\"):
        return Path("\\\\?\\" + str(resolved))
    return resolved


def _git_path(path: Path) -> str:
    value = str(path)
    # Git for Windows implements long paths itself; it rejects verbatim arguments.
    return value[4:] if value.startswith("\\\\?\\") else value


class ReviewWorktrees:
    """子会话的显式工作树创建与三方合入；不承担日常文件回退。"""

    def __init__(self, root: Path, storage: Path, *,
                 excluded_roots: tuple[Path, ...] = (), ref: str = "HEAD",
                 ignore_root: Path | None = None, symlinks: bool = True) -> None:
        self.root = _native_path(root)
        self.storage = _native_path(storage)
        self.repository = self.storage / "repository.git"
        self.ref = ref
        self.symlinks = symlinks
        self.ignore_root = self.root if ignore_root is None else _native_path(ignore_root)
        self.excluded_roots = (self.storage, *(_native_path(path) for path in excluded_roots))

    async def record(self) -> str:
        return await self._run(self._record)

    async def fork(self, root: Path, ref: str, *, conflict_from: tuple[str, str] | None = None) -> str:
        """冻结一次基准，原子发布工作树；重试不重置已经开始的子。"""
        return await self._run(lambda index: self._fork(index, root, ref, conflict_from))

    async def compare(self, base: str, version: str, paths: tuple[str, ...] = ()) -> dict:
        def operation(index):
            files = self._git(index, "diff", "--no-renames", "--name-status", "-z", base, version, "--")
            fields = files.decode("utf-8").split("\0")[:-1]
            changes = [dict(status=fields[i], path=fields[i + 1]) for i in range(0, len(fields), 2)]
            result = {"files": changes}
            if paths:
                patch = self._git(index, "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv",
                                  "--no-renames", base, version, "--", *paths).decode("utf-8", errors="replace")
                result.update(diff=patch[:120_000], truncated=len(patch) > 120_000)
                result["limitations"] = ["binary 文件只提供变化摘要，不含正文"] if "Binary files " in patch else []
            return result
        return await self._run(operation)

    async def merge(self, base: str, version: str) -> tuple[str, ...]:
        def operation(index):
            current = self._record(index)
            tree, conflicts = self._merge_tree(index, base, current, version)
            if not conflicts:
                self._git(index, "read-tree", "--reset", "-u", tree)
            return conflicts
        return await self._run(operation)

    def _merge_tree(self, index, base, current, version):
        output = self._git(index, "merge-tree", "--write-tree", "--name-only", "-z",
                           f"--merge-base={base}", current, version, accepted=(0, 1))
        fields = output.split(b"\0")
        tree = fields[0].decode().strip()
        conflicts = []
        for path in fields[1:]:
            if not path:
                break
            conflicts.append(path.decode("utf-8"))
        return tree, tuple(conflicts)

    def _fork(self, index, root, ref, conflict_from):
        base_ref = ref + "-base"
        base = self._git(index, "rev-parse", "--verify", "--quiet", base_ref,
                         accepted=(0, 1)).decode().strip()
        if not base:
            base = self._record(index)
            self._git(index, "update-ref", base_ref, base, "0" * 40)
        root = _native_path(root)
        child = ReviewWorktrees(root, self.storage, ref=ref, ignore_root=self.ignore_root)
        if not root.exists():
            root.parent.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(dir=root.parent) as temporary:
                published = Path(temporary) / "tree"
                published.mkdir()
                staging = ReviewWorktrees(published, self.storage, ref=ref,
                                            ignore_root=self.ignore_root)
                tree = base
                if conflict_from is not None:
                    tree, _ = self._merge_tree(index, conflict_from[0], base, conflict_from[1])
                staging._git(index, "read-tree", tree)
                staging._git(index, "checkout-index", "--all", "--force")
                os.rename(published, root)
        if child._head(index) is None:
            self._git(index, "update-ref", ref, base, "0" * 40)
        return base

    async def _run(self, operation):
        task = asyncio.create_task(asyncio.to_thread(self._locked, operation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            # 不让后台线程在 Worker 关闭后继续改文件。
            try:
                await task
            except BaseException as error:
                raise BaseExceptionGroup("workspace operation failed during cancellation",
                                         [cancelled, error]) from None
            raise

    def _locked(self, operation):
        self.storage.mkdir(parents=True, exist_ok=True)
        # 只串行化版本库事务，不协调 Session 对工作树的普通写入。
        try:
            with closing(sqlite3.connect(self.storage / "lock.sqlite", timeout=30)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if not self.repository.exists():
                    self._git(None, "init", "--bare", "--template=", _git_path(self.repository))
                (self.repository / "info").mkdir(exist_ok=True)
                (self.repository / "info" / "attributes").write_text(
                    "* -text -filter -ident -working-tree-encoding\n", encoding="utf-8",
                )
                with TemporaryDirectory(dir=self.storage) as temporary:
                    return operation(Path(temporary) / "index")
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode in {
                sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_CANTOPEN,
                sqlite3.SQLITE_READONLY, sqlite3.SQLITE_FULL,
            }:
                raise VersionBackendError(str(error)) from error
            raise

    def _git(self, index: Path | None, *args: str, data: bytes | None = None,
             accepted: tuple[int, ...] = (0,)) -> bytes:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "RED PANDA", "GIT_AUTHOR_EMAIL": "redpanda@local",
            "GIT_COMMITTER_NAME": "RED PANDA", "GIT_COMMITTER_EMAIL": "redpanda@local",
        })
        command = ["git", "-c", "core.longpaths=true"]
        if index is not None:
            env["GIT_INDEX_FILE"] = _git_path(index)
            command += [
                f"--git-dir={_git_path(self.repository)}", f"--work-tree={_git_path(self.root)}",
                "-c", "core.bare=false", "-c", "core.autocrlf=false",
                "-c", "core.safecrlf=false", "-c", "core.quotePath=false",
                "-c", f"core.symlinks={str(self.symlinks).lower()}",
                "-c", f"core.excludesFile={_git_path(self.ignore_root / '.git' / 'info' / 'exclude')}",
            ]
        result = subprocess.run(
            [*command, *args], input=data, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=_git_path(self.root), env=env,
        )
        if result.returncode not in accepted:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            # 损坏和未知 Git 错误不是可继续的「记录不可用」。
            known = ("Permission denied", "Access is denied", "No space left on device",
                     "Read-only file system", "unable to access", "could not open",
                     "Unable to create", "unable to create", "cannot stat",
                     "unable to unlink", "unable to write", "could not write",
                     "cannot create directory", "File name too long", "Filename too long")
            if any(fragment in message for fragment in known):
                raise VersionBackendError(message)
            raise RuntimeError(f"workspace Git {args[0]} failed: {message}")
        return result.stdout

    def _head(self, index: Path) -> str | None:
        value = self._git(index, "rev-parse", "--verify", "--quiet", self.ref, accepted=(0, 1))
        return value.decode().strip() or None

    def _walk(self, index: Path) -> list[bytes]:
        """逐层收集要记录的路径；忽略的目录在下降之前就被剪掉。

        忽略规则必须先于遍历生效：被忽略的目录不进快照，它读不读得动都
        与记录无关。反过来，要记录的内容读不动就是真的记不成，异常照常
        上抛。每层一次判定，深度决定调用次数，不随文件数增长。
        """
        included: list[bytes] = []
        level = [self.root]
        while level:
            entries: list[tuple[bytes, bool]] = []
            for parent in level:
                with os.scandir(parent) as scan:
                    for entry in scan:
                        if entry.name.casefold() == ".git":
                            continue
                        native = Path(entry.path)
                        if native in self.excluded_roots:
                            continue
                        entries.append((
                            native.relative_to(self.root).as_posix().encode("utf-8"),
                            # 符号链接目录记成条目本身，不跟进去。
                            entry.is_dir(follow_symlinks=False),
                        ))
            if not entries:
                break
            ignored = set(self._git(
                index, "check-ignore", "--no-index", "-z", "--stdin",
                data=b"\0".join(path for path, _ in entries) + b"\0", accepted=(0, 1),
            ).split(b"\0"))
            level = []
            for path, descend in entries:
                if path in ignored:
                    continue
                if descend:
                    level.append(self.root / path.decode("utf-8"))
                else:
                    included.append(path)
        return included

    def _record(self, index: Path) -> str:
        previous = self._head(index)
        self._git(index, "read-tree", "--empty")
        included = self._walk(index)
        if included:
            # Plumbing 按原始字节存储；不执行工作树的 filter，也不把嵌套仓库变成 gitlink。
            regular = [path for path in included
                       if not (self.root / path.decode("utf-8")).is_symlink()]
            hashes = self._git(
                index, "hash-object", "-w", "--no-filters", "--stdin-paths",
                data="".join(json.dumps(_git_path(self.root / path.decode("utf-8")), ensure_ascii=False) + "\n"
                             for path in regular).encode("utf-8"),
            ).splitlines() if regular else []
            objects = dict(zip(regular, hashes, strict=True))
            entries = []
            for path in included:
                native = self.root / path.decode("utf-8")
                if native.is_symlink():
                    mode = b"120000"
                    oid = self._git(index, "hash-object", "-w", "--stdin",
                                    data=os.fsencode(os.readlink(native))).strip()
                else:
                    mode = b"100755" if native.stat().st_mode & 0o111 else b"100644"
                    oid = objects[path]
                entries.append(mode + b" " + oid + b"\t" + path + b"\0")
            self._git(index, "update-index", "-z", "--index-info", data=b"".join(entries))
        tree = self._git(index, "write-tree").decode().strip()
        if previous is not None:
            old_tree = self._git(index, "rev-parse", f"{previous}^{{tree}}").decode().strip()
            if tree == old_tree:
                return previous
        parent = [] if previous is None else ["-p", previous]
        version = self._git(index, "commit-tree", tree, *parent,
                            data=b"SubAgent review baseline\n").decode().strip()
        self._git(index, "update-ref", self.ref, version, previous or "0" * 40)
        return version
