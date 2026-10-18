"""Host-owned Git worktrees for long-lived coding Agents."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import subprocess
from typing import NoReturn

from redpanda.filesystem.registry import WorkspaceRecord
from redpanda.paths import RedPandaHome


class AgentProjectRequired(ValueError):
    """The selected project cannot provide a committed Git worktree."""


@dataclass(frozen=True, slots=True)
class GitWorktree:
    path: Path
    branch: str


def _key(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:20]


class AgentWorktrees:
    def __init__(self, home: RedPandaHome, environment: dict[str, str]):
        self.root = (home.state_root / "coding_bot").resolve()
        owner = _key(os.path.normcase(str(home.root)))
        self.namespace = f"redpanda/{owner}"
        # Repository selection must come from the registered project, never the
        # shell that happened to launch the Host.
        self.environment = {key: value for key, value in environment.items()
                            if not key.upper().startswith("GIT_")}
        self.environment["LC_ALL"] = "C"

    def integration(self, workspace_id: str) -> GitWorktree:
        project = _key(workspace_id)
        return GitWorktree(self.root / project / "integration",
                           f"{self.namespace}/{project}/integration")

    def agent(self, workspace_id: str, session_id: str) -> GitWorktree:
        project, agent = _key(workspace_id), _key(session_id)
        return GitWorktree(self.root / project / agent,
                           f"{self.namespace}/{project}/{agent}")

    def create(self, project: WorkspaceRecord, session_id: str) -> GitWorktree:
        self._require_project(project.task_root)
        integration = self.integration(project.workspace_id)
        if self._has_branch(project.task_root, integration.branch):
            self._validate(project.task_root, integration)
        else:
            self._add(project.task_root, integration, self._initial_commit(project.task_root))
        agent = self.agent(project.workspace_id, session_id)
        self._add(project.task_root, agent, integration.branch)
        return agent

    def remove(self, project: WorkspaceRecord, worktree: GitWorktree) -> None:
        # Only used to roll back a worktree whose Agent fact was not committed.
        # Git refuses removal when it finds user changes; never force deletion.
        self._git(project.task_root, "worktree", "remove", str(worktree.path))
        self._git(project.task_root, "branch", "-D", worktree.branch)

    def _require_project(self, root: Path) -> None:
        result = self._git(root, "rev-parse", "--is-inside-work-tree", accepted=(0, 128))
        if result.returncode == 128:
            if b"not a git repository (or any of the parent directories)" in result.stderr:
                raise AgentProjectRequired("项目必须是 Git 仓库，请先初始化并创建首个提交。")
            self._raise(result)
        if result.stdout.strip() != b"true":
            raise AgentProjectRequired("项目必须是 Git 工作树，不能是裸仓库。")
        top = Path(self._git(root, "rev-parse", "--show-toplevel").stdout.decode("utf-8").rstrip("\r\n")).resolve()
        if top != root:
            raise AgentProjectRequired("项目路径必须是 Git 工作树根目录，请登记仓库根目录。")
        if self.root.is_relative_to(root):
            raise AgentProjectRequired("RED PANDA 数据目录必须位于项目工作树之外。")

    def _initial_commit(self, root: Path) -> str:
        head = self._git(root, "symbolic-ref", "--quiet", "HEAD", accepted=(0, 1))
        if head.returncode == 0:
            branch = head.stdout.decode("utf-8").strip()
            exists = self._git(root, "show-ref", "--verify", "--quiet", branch, accepted=(0, 1))
            if exists.returncode == 1:
                raise AgentProjectRequired("项目尚无提交，请先创建首个 Git 提交。")
        return self._git(root, "rev-parse", "--verify", "HEAD^{commit}").stdout.decode("ascii").strip()

    def _has_branch(self, root: Path, branch: str) -> bool:
        return self._git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}",
                         accepted=(0, 1)).returncode == 0

    def _validate(self, root: Path, worktree: GitWorktree) -> None:
        top = self._git(worktree.path, "rev-parse", "--show-toplevel").stdout.decode("utf-8").rstrip("\r\n")
        branch = self._git(worktree.path, "symbolic-ref", "HEAD").stdout.decode("utf-8").strip()
        common = self._common_dir(root)
        actual = self._common_dir(worktree.path)
        if (Path(top).resolve() != worktree.path or branch != f"refs/heads/{worktree.branch}"
                or actual != common):
            raise RuntimeError("persisted integration worktree does not match its project and branch")

    def _common_dir(self, root: Path) -> Path:
        value = self._git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout
        return Path(value.decode("utf-8").rstrip("\r\n")).resolve()

    def _add(self, root: Path, worktree: GitWorktree, base: str) -> None:
        worktree.path.parent.mkdir(parents=True, exist_ok=True)
        # Create the ref separately, so failure cleanup owns exactly this ref.
        oid = self._git(root, "rev-parse", "--verify", f"{base}^{{commit}}").stdout.decode("ascii").strip()
        self._git(root, "update-ref", f"refs/heads/{worktree.branch}", oid, "0" * len(oid))
        try:
            self._git(root, "worktree", "add", str(worktree.path), worktree.branch)
        except BaseException as error:
            try:
                self._git(root, "update-ref", "-d", f"refs/heads/{worktree.branch}", oid)
            except BaseException as cleanup_error:
                raise BaseExceptionGroup("worktree creation and ref cleanup failed", [error, cleanup_error]) from None
            raise

    def _git(self, root: Path, *arguments: str, accepted: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            ["git", "-c", "core.longpaths=true", "-c", f"core.hooksPath={os.devnull}",
             "-C", str(root), *arguments],
            env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode not in accepted:
            self._raise(result)
        return result

    @staticmethod
    def _raise(result: subprocess.CompletedProcess[bytes]) -> NoReturn:
        raise subprocess.CalledProcessError(result.returncode, result.args,
                                            output=result.stdout, stderr=result.stderr)
