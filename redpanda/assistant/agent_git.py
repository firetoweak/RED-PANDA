"""Git invocations owned by RED PANDA for Coding Bot worktrees."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Literal, NoReturn


AUTHOR_EMAIL = "bot@redpanda.local"


@dataclass(frozen=True, slots=True)
class GitWorkspace:
    """A real worktree a Session runs in directly, and whose Git identity it uses."""
    worktree: Path
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
        result = subprocess.run(
            ["git", "-c", "core.longpaths=true", "-c", f"core.hooksPath={os.devnull}",
             "-c", "commit.gpgsign=false", "-C", str(root), *arguments],
            env=self.environment if environment is None else {**self.environment, **environment},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
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
