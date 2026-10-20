"""Host-owned Git worktrees for long-lived coding Agents."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import subprocess

from redpanda.assistant.agent_git import Git, MergeResult, git_error, identity, raise_git
from redpanda.assistant.agents import AgentRecord
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


def project_worktrees(home: RedPandaHome, workspace_id: str) -> Path:
    """Every worktree of one project's Agents, their children and integration lives here."""
    return (home.state_root / "coding_bot").resolve() / _key(workspace_id)


class AgentWorktrees:
    def __init__(self, home: RedPandaHome, environment: dict[str, str]):
        self.root = home.agent_worktrees_root.resolve()
        owner = _key(os.path.normcase(str(home.root)))
        self.namespace = f"redpanda/{owner}"
        self.git = Git(environment)

    def integration(self, workspace_id: str) -> GitWorktree:
        project = _key(workspace_id)
        return GitWorktree(self.root / project / "integration",
                           f"{self.namespace}/{project}/integration")

    def agent(self, workspace_id: str, session_id: str) -> GitWorktree:
        project, agent = _key(workspace_id), _key(session_id)
        return GitWorktree(self.root / project / agent,
                           f"{self.namespace}/{project}/{agent}")

    def child(self, workspace_id: str, child_session_id: str) -> GitWorktree:
        project, child = _key(workspace_id), _key(child_session_id)
        return GitWorktree(self.root / project / "children" / child,
                           f"{self.namespace}/{project}/children/{child}")

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

    def land(self, agent: AgentRecord) -> MergeResult:
        """Merge the Agent's committed branch into the project integration branch.

        Callers serialize landings per project. A conflict or files in the integration
        worktree that the merge would overwrite leave integration untouched.
        """
        integration = self.integration(agent.workspace_id)
        return self.git.merge(
            integration.path, self.git.oid(integration.path, f"refs/heads/{agent.project.branch}"),
            author=agent.name, message=f"合入 {agent.name} 的工作", keep_conflict=False,
        )

    def sync(self, agent: AgentRecord) -> MergeResult:
        """Merge landed work into the Agent branch; conflicts stay for the Agent to resolve."""
        if self.git.merging(agent.worktree):
            return MergeResult("in_progress", self.git.oid(agent.worktree, "HEAD"))
        integration = self.integration(agent.workspace_id)
        return self.git.merge(
            agent.worktree, self.git.oid(agent.worktree, f"refs/heads/{integration.branch}"),
            author=agent.name, message="同步已落地的工作", keep_conflict=True,
        )

    def create_child(self, project: WorkspaceRecord, parent: AgentRecord, child_session_id: str,
                     conflict_source: str | None = None) -> GitWorktree:
        """Branch a delegated task from the parent's committed work; replay reuses it.

        A conflict-resolving task starts with the failed merge in progress, so its
        resolution is completed as that merge when the task returns.
        """
        child = self.child(parent.workspace_id, child_session_id)
        if self._has_branch(project.task_root, child.branch):
            self._validate(project.task_root, child)
        else:
            self._add(project.task_root, child, parent.project.branch)
        if conflict_source is not None and not self.git.merging(child.path):
            source = self.child(parent.workspace_id, conflict_source)
            result = self.git.merge(child.path, self.git.oid(child.path, f"refs/heads/{source.branch}"),
                                    author=parent.name, message="合入待解决冲突的子任务成果", keep_conflict=True)
            if result.status in {"blocked", "failed"}:
                raise RuntimeError(f"conflict-resolving task could not start its merge: {result.detail}")
        return child

    def finish_child(self, parent: AgentRecord, child_session_id: str) -> str | None:
        """Fix the returned task's result as one commit on its branch.

        The task cannot run commands, so it cannot commit itself; its branch is
        only the vehicle the parent compares and merges. A conflict-resolving
        task's result completes the merge it started in. Returns Git's own error
        when Git fails, such as on a leftover index.lock.
        """
        child = self.child(parent.workspace_id, child_session_id)
        added = self.git.run(child.path, "add", "--all", accepted=(0, 128))
        if added.returncode:
            return git_error(added)
        merging = self.git.merging(child.path)
        if merging or self.git.run(child.path, "diff", "--cached", "--quiet", accepted=(0, 1)).returncode == 1:
            committed = self.git.run(child.path, "commit", "--quiet", "--no-verify",
                                     *(("--no-edit",) if merging else ("-m", "子任务成果")),
                                     environment=identity(parent.name), accepted=(0, 1, 128))
            if committed.returncode:
                return git_error(committed)
        return None

    def compare_child(self, parent: AgentRecord, child_session_id: str,
                      paths: tuple[str, ...], offset: int) -> dict:
        child = self.child(parent.workspace_id, child_session_id)
        return self.git.compare(parent.worktree, f"refs/heads/{parent.project.branch}",
                                f"refs/heads/{child.branch}", paths, offset=offset)

    def merge_child(self, parent: AgentRecord, child_session_id: str) -> tuple[MergeResult, tuple[str, ...]]:
        """Merge a returned task into the parent branch; a conflict changes nothing."""
        if self.git.merging(parent.worktree):
            return MergeResult("in_progress", self.git.oid(parent.worktree, "HEAD")), ()
        child = self.child(parent.workspace_id, child_session_id)
        before = self.git.oid(parent.worktree, "HEAD")
        result = self.git.merge(parent.worktree, self.git.oid(parent.worktree, f"refs/heads/{child.branch}"),
                                author=parent.name, message="合入子任务的成果", keep_conflict=False)
        if result.status != "merged":
            return result, ()
        return result, self.git.changed_paths(parent.worktree, before, result.head)

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
            raise_git(result)
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
        return self.git.run(root, *arguments, accepted=accepted)
