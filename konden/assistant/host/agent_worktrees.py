"""Host-owned Git worktrees for the projects linked to long-lived Bots."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
import json
from pathlib import Path
import subprocess

from konden.assistant.agent_git import Git, MergeResult, git_error, raise_git
from konden.assistant.agents import AgentRecord, ProjectLinked
from konden.filesystem.registry import WorkspaceRecord
from konden.paths import KondenHome
from konden.config import write_json


class AgentProjectRequired(ValueError):
    """The selected project cannot provide a committed Git worktree."""


@dataclass(frozen=True, slots=True)
class GitWorktree:
    path: Path
    branch: str


@dataclass(frozen=True, slots=True)
class UnlinkBlocked:
    """Work that unlinking would lose; nothing was removed."""
    uncommitted: tuple[str, ...]
    unlanded: int
    merging: bool
    git: str | None = None


def _key(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:20]


class AgentWorktrees:
    def __init__(self, home: KondenHome, environment: dict[str, str]):
        self.root = home.agent_worktrees_root.resolve()
        owner = _key(os.path.normcase(str(home.root)))
        self.namespace = f"konden/{owner}"
        self.git = Git(environment)

    def integration(self, workspace_id: str) -> GitWorktree:
        project = _key(workspace_id)
        return GitWorktree(self.root / project / "integration",
                           f"{self.namespace}/{project}/integration")

    def agent_branch(self, workspace_id: str, session_id: str) -> str:
        return f"{self.namespace}/{_key(workspace_id)}/{_key(session_id)}"

    def link(self, project: WorkspaceRecord, session_id: str, path: Path) -> GitWorktree:
        """Give one Bot its own branch of the project, checked out at `path`.

        The branch starts from the project's integration branch, which is created
        from the project's HEAD the first time any Bot links it.
        """
        self.require_project(project.task_root)
        integration = self.integration(project.workspace_id)
        base = (integration.branch if self._has_branch(project.task_root, integration.branch)
                else self._initial_commit(project.task_root))
        self._add(project.task_root, integration, base)
        agent = GitWorktree(path, self.agent_branch(project.workspace_id, session_id))
        self._add(project.task_root, agent, integration.branch)
        return agent

    def unlink(self, project: WorkspaceRecord, linked: ProjectLinked, *, recovering: bool = False) -> UnlinkBlocked | None:
        """Remove a Bot's worktree and branch, only when nothing on them would be lost."""
        worktree = Path(linked.worktree)
        if recovering and not worktree.exists():
            if self._has_branch(project.task_root, linked.branch):
                integration = self.integration(linked.workspace_id).branch
                unlanded = int(self._git(project.task_root, "rev-list", "--count",
                    f"refs/heads/{integration}..refs/heads/{linked.branch}").stdout)
                if unlanded:
                    raise RuntimeError("removed project worktree still has unlanded commits")
                self._git(project.task_root, "branch", "-D", linked.branch)
            return None
        status = self._git(worktree, "status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all").stdout
        uncommitted = tuple(entry[3:].decode("utf-8") for entry in status.split(b"\0") if len(entry) > 3)
        integration = self.integration(linked.workspace_id).branch
        unlanded = int(self._git(worktree, "rev-list", "--count",
                                 f"refs/heads/{integration}..refs/heads/{linked.branch}").stdout.decode("ascii"))
        merging = self.git.merging(worktree)
        if uncommitted or unlanded or merging:
            return UnlinkBlocked(uncommitted, unlanded, merging)
        # Something written beside this call after the check makes Git refuse; nothing is lost.
        removed = self._git(project.task_root, "worktree", "remove", str(worktree), accepted=(0, 128))
        if removed.returncode:
            return UnlinkBlocked(uncommitted, unlanded, merging, git_error(removed))
        self._git(project.task_root, "branch", "-D", linked.branch)
        return None

    def land(self, agent: AgentRecord, linked: ProjectLinked) -> MergeResult:
        """Merge the Bot's committed branch into the project integration branch.

        Callers serialize landings per project. A conflict or files in the integration
        worktree that the merge would overwrite leave integration untouched.
        """
        integration = self.integration(linked.workspace_id)
        return self.git.merge(
            integration.path, self.git.oid(integration.path, f"refs/heads/{linked.branch}"),
            author=agent.name, message=f"合入 {agent.name} 的工作", keep_conflict=False,
        )

    def sync(self, agent: AgentRecord, linked: ProjectLinked) -> MergeResult:
        """Merge landed work into the Bot branch; conflicts stay for the Bot to resolve."""
        worktree = Path(linked.worktree)
        if self.git.merging(worktree):
            return MergeResult("in_progress", self.git.oid(worktree, "HEAD"))
        integration = self.integration(linked.workspace_id)
        return self.git.merge(
            worktree, self.git.oid(worktree, f"refs/heads/{integration.branch}"),
            author=agent.name, message="同步已落地的工作", keep_conflict=True,
        )

    def remove(self, project: WorkspaceRecord, worktree: GitWorktree) -> None:
        # Only used to roll back a worktree whose link fact was not committed.
        # Git refuses removal when it finds user changes; never force deletion.
        self._git(project.task_root, "worktree", "remove", str(worktree.path))
        self._git(project.task_root, "branch", "-D", worktree.branch)

    def require_project(self, root: Path) -> None:
        result = self._git(root, "rev-parse", "--is-inside-work-tree", accepted=(0, 128))
        if result.returncode == 128:
            if (b"not a git repository (or any of the parent directories)" in result.stderr
                    or b"not a git repository (or any parent up to mount point " in result.stderr):
                raise AgentProjectRequired("项目必须是 Git 仓库，请先初始化并创建首个提交。")
            raise_git(result)
        if result.stdout.strip() != b"true":
            raise AgentProjectRequired("项目必须是 Git 工作树，不能是裸仓库。")
        top = Path(self._git(root, "rev-parse", "--show-toplevel").stdout.decode("utf-8").rstrip("\r\n")).resolve()
        if top != root:
            raise AgentProjectRequired("项目路径必须是 Git 工作树根目录，请登记仓库根目录。")
        if self.root.is_relative_to(root):
            raise AgentProjectRequired("应用数据目录必须位于项目工作树之外。")

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
        pending = self.root / "creating" / f"{_key(worktree.branch)}.json"
        if pending.exists():
            intent = json.loads(pending.read_text(encoding="utf-8"))
            if intent["path"] != str(worktree.path) or intent["branch"] != worktree.branch:
                raise ValueError("worktree creation conflicts with its original intent")
            oid = intent["oid"]
        elif self._has_branch(root, worktree.branch):
            self._validate(root, worktree)
            return
        else:
            oid = self.git.oid(root, base)
            write_json(pending, dict(path=str(worktree.path), branch=worktree.branch, oid=oid))
        worktree.path.parent.mkdir(parents=True, exist_ok=True)
        if self._has_branch(root, worktree.branch):
            if self.git.oid(root, f"refs/heads/{worktree.branch}") != oid:
                raise ValueError("unfinished worktree branch moved from its initial commit")
        else:
            self._git(root, "update-ref", f"refs/heads/{worktree.branch}", oid, "0" * len(oid))
        if (worktree.path / ".git").is_file():
            self._validate(root, worktree)
            # This unpublished tree has never been handed to a Bot. Complete only
            # the checkout of the commit frozen before its creation.
            self._git(worktree.path, "reset", "--hard", oid)
            pending.unlink()
            return
        try:
            self._git(root, "worktree", "add", str(worktree.path), worktree.branch)
        except BaseException as error:
            try:
                self._git(root, "update-ref", "-d", f"refs/heads/{worktree.branch}", oid)
                pending.unlink()
            except BaseException as cleanup_error:
                raise BaseExceptionGroup("worktree creation and ref cleanup failed", [error, cleanup_error]) from None
            raise
        pending.unlink()

    def _git(self, root: Path, *arguments: str, accepted: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[bytes]:
        return self.git.run(root, *arguments, accepted=accepted)
