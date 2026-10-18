"""Git invocations owned by RED PANDA for Coding Bot worktrees."""
from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import subprocess
from typing import NoReturn


AUTHOR_EMAIL = "agent@redpanda.local"


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
             "-C", str(root), *arguments],
            env=self.environment if environment is None else {**self.environment, **environment},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode not in accepted:
            raise_git(result)
        return result

    def commit_all(self, root: Path, author: str) -> str:
        """Commit every non-ignored change and return the resulting HEAD."""
        # Most Steps change nothing; one status call is the whole cost then.
        status = self.run(root, "status", "--porcelain=v2", "--branch", "--untracked-files=all", "-z").stdout
        entries = [entry for entry in status.split(b"\0") if entry]
        if all(entry.startswith(b"# ") for entry in entries):
            return next(entry[len(b"# branch.oid "):] for entry in entries
                        if entry.startswith(b"# branch.oid ")).decode("ascii")
        self.run(root, "add", "--all")
        if self.run(root, "diff", "--cached", "--quiet", accepted=(0, 1)).returncode == 1:
            identity = {"GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
                        "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL}
            self.run(root, "-c", "commit.gpgsign=false", "commit", "--quiet", "--no-verify",
                     "-m", "自动提交", environment=identity)
        return self.run(root, "rev-parse", "--verify", "HEAD^{commit}").stdout.decode("ascii").strip()


def raise_git(result: subprocess.CompletedProcess[bytes]) -> NoReturn:
    raise subprocess.CalledProcessError(result.returncode, result.args,
                                        output=result.stdout, stderr=result.stderr)
