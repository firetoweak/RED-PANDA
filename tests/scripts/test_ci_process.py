"""干净验证只包含提交内容，成功和失败后都释放临时 checkout。"""
import subprocess

import pytest

from scripts import ci


pytestmark = pytest.mark.process


@pytest.mark.parametrize("exit_code", [0, 7])
def test_clean_checkout_excludes_local_artifacts_and_is_always_removed(tmp_path, monkeypatch, exit_code):
    repository = tmp_path / "repository"
    (repository / "scripts").mkdir(parents=True)
    (repository / "scripts/ci.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "assert not Path('web/dist').exists()\n"
        "assert Path(sys.prefix).resolve() == Path('konden-env').resolve()\n"
        "raise SystemExit(int(sys.argv[1]))\n", encoding="utf-8")
    monkeypatch.setattr(ci, "ROOT", repository)
    ci.run(["git", "init"])
    ci.run(["git", "add", "scripts/ci.py"])
    ci.run(["git", "-c", "user.name=CI Test", "-c", "user.email=ci@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-m", "fixture"])
    (repository / "web/dist").mkdir(parents=True)
    if exit_code:
        with pytest.raises(subprocess.CalledProcessError) as error:
            ci.clean_checkout([str(exit_code)])
        assert error.value.returncode == exit_code
    else:
        ci.clean_checkout([str(exit_code)])
    worktrees = subprocess.check_output(["git", "worktree", "list", "--porcelain"],
                                        cwd=repository, text=True)
    assert worktrees.count("worktree ") == 1
    assert (repository / "web/dist").is_dir()
