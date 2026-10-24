"""Git invocations owned by konden for Coding Bot worktrees."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from typing import Literal, NoReturn

from konden.config import write_json


AUTHOR_EMAIL = "bot@konden.local"


@dataclass(frozen=True, slots=True)
class GitWorkspace:
    """The Git identity a Session commits with, and the project copy it runs in, if any.

    A Bot runs in its own directory and mounts its projects there; only a task
    delegated into a project runs directly in that project copy.
    """
    worktree: Path | None
    author: str


@dataclass(frozen=True, slots=True)
class MergeResult:
    status: Literal["merged", "unchanged", "conflict", "blocked", "failed", "in_progress"]
    head: str
    fast_forward: bool = False
    files: tuple[str, ...] = ()
    detail: str = ""


class Git:
    def __init__(self, environment: Mapping[str, str]):
        # Repository selection must come from the given directory, never the
        # shell that happened to launch the process.
        self.environment = {key: value for key, value in environment.items()
                            if not key.upper().startswith("GIT_")}
        self.environment["LC_ALL"] = "C"

    def run(self, root: Path, *arguments: str, accepted: tuple[int, ...] = (0,),
            environment: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
        if sys.platform == "win32":
            from konden.execution.process_win import Running
        elif sys.platform == "linux":
            from konden.execution.process_linux import Running
        else:
            raise RuntimeError("Owned Git processes require Windows or Linux")
        env = self.environment if environment is None else {**self.environment, **environment}
        executable = shutil.which("git", path=env.get("PATH"))
        if executable is None:
            raise FileNotFoundError("git")
        argv = [executable, "-c", "core.longpaths=true", "-c", f"core.hooksPath={os.devnull}",
                "-c", "commit.gpgsign=false", "-C", str(root), *arguments]
        # The same process owner as commands, without command/background semantics.
        # Keep exact bytes: Git's NUL-delimited output must not pass through text capture.
        with TemporaryDirectory(prefix="konden-git-") as directory:
            running = Running(argv, root, env, directory)
            try:
                while (code := running.poll()) is None:
                    time.sleep(.01)
            finally:
                running.close()
            result = subprocess.CompletedProcess(argv, code,
                (Path(directory) / "stdout.bin").read_bytes(),
                (Path(directory) / "stderr.bin").read_bytes())
        if result.returncode not in accepted:
            raise_git(result)
        return result

    def oid(self, root: Path, revision: str) -> str:
        return self.run(root, "rev-parse", "--verify", f"{revision}^{{commit}}").stdout.decode("ascii").strip()

    def merging(self, root: Path) -> bool:
        return self.run(root, "rev-parse", "-q", "--verify", "MERGE_HEAD", accepted=(0, 1)).returncode == 0

    def merge(self, root: Path, source: str, *, author: str, message: str,
              keep_conflict: bool) -> MergeResult:
        """Merge commit `source` into the branch checked out at `root`."""
        recovered = self.recover_merge(root)
        if recovered is not None and recovered.status not in {"merged", "unchanged"}:
            return recovered
        if self.merging(root):
            return MergeResult("in_progress", self.oid(root, "HEAD"))
        pending = self._merge_path(root)
        write_json(pending, dict(head=self.oid(root, "HEAD"), source=source, author=author,
                                 message=message, keep_conflict=keep_conflict))
        result = self._merge(root, source, author=author, message=message, keep_conflict=keep_conflict)
        pending.unlink()
        return result

    def _merge_path(self, root: Path) -> Path:
        return Path(self.run(root, "rev-parse", "--path-format=absolute", "--git-path",
                             "konden-merge.json").stdout.decode("utf-8").strip())

    def recover_merge(self, root: Path) -> MergeResult | None:
        pending = self._merge_path(root)
        if not pending.exists():
            return None
        intent = json.loads(pending.read_text(encoding="utf-8"))
        head, source = intent["head"], intent["source"]
        current = self.oid(root, "HEAD")
        if current != head:
            if not self._is_ancestor(root, head, current) or not self._is_ancestor(root, source, current):
                raise ValueError("unfinished merge destination changed unexpectedly")
            if self.merging(root):
                if self.oid(root, "MERGE_HEAD") != source:
                    raise ValueError("completed merge has a different unfinished source")
                self.run(root, "merge", "--quit")
            result = MergeResult("merged", current, current == source)
        elif self.merging(root):
            if self.oid(root, "MERGE_HEAD") != source:
                raise ValueError("unfinished merge source differs from its accepted operation")
            files = tuple(path.decode("utf-8") for path in self.run(
                root, "diff", "--name-only", "--diff-filter=U", "-z").stdout.split(b"\0") if path)
            if files:
                if not intent["keep_conflict"]:
                    self.run(root, "merge", "--abort")
                result = MergeResult("conflict", head, files=files)
            else:
                # Verify the prepared tree is exactly Git's original automatic merge.
                # A manually changed index is never committed by recovery.
                expected = self.run(root, "merge-tree", "--write-tree", head, source, accepted=(0, 1))
                actual = self.run(root, "write-tree").stdout.strip()
                if expected.returncode or expected.stdout.splitlines()[0] != actual:
                    raise ValueError("unfinished merge tree differs from the original automatic merge")
                self.run(root, "commit", "-m", intent["message"], environment=identity(intent["author"]))
                result = MergeResult("merged", self.oid(root, "HEAD"))
        else:
            result = self._merge(root, source, author=intent["author"], message=intent["message"],
                                 keep_conflict=intent["keep_conflict"])
        pending.unlink()
        return result

    def _merge(self, root: Path, source: str, *, author: str, message: str,
               keep_conflict: bool) -> MergeResult:
        head = self.oid(root, "HEAD")
        if self._is_ancestor(root, source, head):
            return MergeResult("unchanged", head)
        fast_forward = self._is_ancestor(root, head, source)
        # 128 is Git's own fatal error, such as a leftover index.lock.
        result = self.run(root, "merge", "--no-edit", "-m", message, source,
                          accepted=(0, 1, 2, 128), environment=identity(author))
        if result.returncode == 0:
            return MergeResult("merged", self.oid(root, "HEAD"), fast_forward)
        if not self.merging(root):
            detail = git_error(result)
            # Only files the merge would overwrite are a refusal we name; any other
            # failure goes back with Git's own words. The exit code of a refusal
            # differs between fast-forward and real merges, and Git indents the
            # listed files with a tab or spaces depending on the merge.
            if "would be overwritten by merge" not in detail:
                return MergeResult("failed", head, detail=detail)
            files = tuple(line.strip() for line in detail.splitlines() if line[:1].isspace())
            return MergeResult("blocked", head, files=files, detail=detail)
        files = tuple(path.decode("utf-8") for path in self.run(
            root, "diff", "--name-only", "--diff-filter=U", "-z").stdout.split(b"\0") if path)
        if not files:
            raise_git(result)
        if not keep_conflict:
            self.run(root, "merge", "--abort")
        return MergeResult("conflict", head, files=files)

    def changed_paths(self, root: Path, base: str, version: str) -> tuple[str, ...]:
        return tuple(path.decode("utf-8") for path in self.run(
            root, "diff", "--no-renames", "--name-only", "-z", base, version, "--").stdout.split(b"\0") if path)

    def compare(self, root: Path, base: str, version: str, paths: tuple[str, ...] = (), *, offset: int = 0) -> dict:
        """Changes of `version` since its merge base with `base`, readable in pages."""
        fork = self.run(root, "merge-base", base, version).stdout.decode("ascii").strip()
        fields = self.run(root, "diff", "--no-renames", "--name-status", "-z", fork, version, "--"
                          ).stdout.decode("utf-8").split("\0")[:-1]
        result = {"files": [dict(status=fields[i], path=fields[i + 1]) for i in range(0, len(fields), 2)]}
        if paths:
            patch = self.run(root, "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                             fork, version, "--", *paths).stdout.decode("utf-8", errors="replace")
            end = offset + 120_000
            result.update(diff=patch[offset:end], truncated=len(patch) > end,
                          offset=offset, next_offset=end if len(patch) > end else None)
            result["limitations"] = ["binary 文件只提供变化摘要，不含正文"] if "Binary files " in patch else []
        return result

    def _is_ancestor(self, root: Path, ancestor: str, descendant: str) -> bool:
        return self.run(root, "merge-base", "--is-ancestor", ancestor, descendant,
                        accepted=(0, 1)).returncode == 0


def identity(author: str) -> dict[str, str]:
    return {"GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL}


def git_error(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stderr.decode("utf-8", "replace").strip()


def raise_git(result: subprocess.CompletedProcess[bytes]) -> NoReturn:
    raise subprocess.CalledProcessError(result.returncode, result.args,
                                        output=result.stdout, stderr=result.stderr)
