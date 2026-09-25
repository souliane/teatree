"""The diff-scoped mutation run names changed modules the way the registry spells them.

The registry lists ``src/teatree/...`` paths. In a fork that vendors core, git reports
the same file as ``vendor/teatree/src/teatree/...``, so an unscoped diff matched nothing
and the run mutated no module while reporting success.
"""

from pathlib import Path

from teatree.quality import mutation_run
from tests._git_repo import make_git_repo, run_git


def test_paths_are_relative_to_the_vendored_core_not_the_fork_root(tmp_path: Path) -> None:
    fork = make_git_repo(tmp_path / "fork")
    core_module = fork / "vendor" / "teatree" / "src" / "teatree" / "quality" / "x.py"
    core_module.parent.mkdir(parents=True)
    run_git(fork, "branch", "base")
    core_module.write_text("VALUE = 1\n", encoding="utf-8")
    (fork / "overlay.py").write_text("VALUE = 2\n", encoding="utf-8")
    run_git(fork, "add", "-A")
    run_git(fork, "commit", "-q", "-m", "change")

    changed = mutation_run.changed_files_vs_main(repo=str(fork / "vendor" / "teatree"), target="base")

    assert changed == ("src/teatree/quality/x.py",)
