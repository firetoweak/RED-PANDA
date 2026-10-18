"""Git invocations owned by RED PANDA for Coding Bot worktrees."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
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

    def review(self, root: Path, base: str, *, path: str | None = None, offset: int = 0) -> dict | None:
        """The worktree's changes, uncommitted ones included, since its merge base with `base`.

        Untracked files are staged into a private copy of the index, so the
        owner's index and `git status` stay untouched.
        """
        fork = self.run(root, "merge-base", base, "HEAD").stdout.decode("ascii").strip()
        index_path = self.run(root, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout
        with TemporaryDirectory() as directory:
            index = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
            shutil.copyfile(index_path.decode("utf-8").rstrip("\r\n"), index["GIT_INDEX_FILE"])
            self.run(root, "add", "--all", environment=index)
            scope = ("--literal-pathspecs", "diff", "--cached", "--no-renames", fork, "--", *([path] if path else []))
            raw = self.run(root, *scope[:-2], "--raw", "-z", *scope[-2:], environment=index).stdout
            counts = self.run(root, *scope[:-2], "--numstat", "-z", *scope[-2:], environment=index).stdout
            patch = (self.run(root, *scope[:-2], "--no-ext-diff", "--no-textconv", *scope[-2:],
                              environment=index).stdout if path else b"")
        reviewed = _review_files(raw, counts)
        files = [file for file, _ in reviewed]
        if path is None:
            return dict(files=files, added=sum(file["added"] or 0 for file in files),
                        removed=sum(file["removed"] or 0 for file in files))
        if not reviewed:
            return None
        lines = _review_lines(patch)
        file, properties = reviewed[0]
        return {**file, "properties": properties, "lines": lines[offset:offset + REVIEW_PAGE_LINES],
                "next_offset": offset + REVIEW_PAGE_LINES if offset + REVIEW_PAGE_LINES < len(lines) else None}

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


REVIEW_PAGE_LINES = 200
_STATUS = {"A": "added", "D": "deleted", "M": "modified", "T": "modified"}


def _review_files(raw: bytes, counts: bytes) -> list[tuple[dict, list[dict]]]:
    fields = raw.split(b"\0")[:-1]
    numbers = {}
    for entry in counts.split(b"\0")[:-1]:
        added, removed, name = entry.split(b"\t", 2)
        numbers[name.decode("utf-8")] = (None, None) if added == b"-" else (int(added), int(removed))
    reviewed = []
    for header, name in zip(fields[::2], fields[1::2]):
        old_mode, new_mode, old_oid, new_oid, status = header.decode("ascii")[1:].split(" ")
        path = name.decode("utf-8")
        added, removed = numbers[path]
        file = dict(path=path, status=_STATUS[status[0]], added=added, removed=removed,
                    binary=added is None)
        # Blob ids and modes identify this exact difference for page continuation.
        file["revision"] = sha256(json.dumps([path, old_mode, new_mode, old_oid, new_oid]).encode()).hexdigest()
        properties = ([dict(text=f"权限：{old_mode} → {new_mode}")]
                      if status[0] == "M" and old_mode != new_mode else [])
        reviewed.append((file, properties))
    return reviewed


def _review_lines(patch: bytes) -> list[dict]:
    rows, old, new = [], None, None
    for raw_line in patch.split(b"\n")[:-1]:
        line = raw_line.decode("utf-8", "replace") + "\n"
        hunk = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if hunk is not None:
            old, new = int(hunk[1]), int(hunk[2])
            rows.append(dict(kind="hunk", old_line=None, new_line=None, text=hunk[0]))
        elif old is None or line.startswith("\\"):
            continue
        elif line.startswith("+"):
            rows.append(dict(kind="add", old_line=None, new_line=new, text=line[1:]))
            new += 1
        elif line.startswith("-"):
            rows.append(dict(kind="delete", old_line=old, new_line=None, text=line[1:]))
            old += 1
        else:
            rows.append(dict(kind="context", old_line=old, new_line=new, text=line[1:]))
            old, new = old + 1, new + 1
    return rows
