# test-path: cross-cutting
"""The deploy build context fast-forwards past lossless dirt, and never past unique work.

`deploy/fast-forward-checkout.sh` replaces the bare `git pull --ff-only` that
wedged the box for 42 commits: a dependabot PR bumped a pin in `pyproject.toml`
without regenerating `uv.lock`, the next `uv run` in the clone re-locked it, and
every deploy from then on aborted with "Your local changes to the following files
would be overwritten by merge" — unreported, so merged code silently stopped
reaching production.

These exercise the real script against real git repos under `tmp_path`. The
safety property under test is content-equivalence: dirt is discarded ONLY when the
working tree already holds the target's exact bytes, so the fast-forward restores
it byte-for-byte. Unique local work is retained and reported, never destroyed.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests._git_repo import git_identity_env, make_git_repo, run_git

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "deploy" / "fast-forward-checkout.sh"
_GIT = shutil.which("git") or "/usr/bin/git"
_BASH = shutil.which("bash") or "/bin/bash"


def _git(cwd: Path, *args: str) -> str:
    return run_git(cwd, *args)


def _run_script(clone: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_BASH, str(_SCRIPT), str(clone)],
        check=False,
        capture_output=True,
        text=True,
        env=git_identity_env(),
    )


def _clone(origin: Path, dest: Path) -> None:
    subprocess.run(
        [_GIT, "clone", "-q", str(origin), str(dest)],
        check=True,
        capture_output=True,
        env=git_identity_env(),
    )


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    """An origin one commit ahead of a clone, mirroring the box's build context."""
    origin = tmp_path / "origin"
    work = tmp_path / "origin-work"
    clone = tmp_path / "clone"

    make_git_repo(origin, bare=True)
    _clone(origin, work)
    (work / "uv.lock").write_text('version = "0.4.10"\n', encoding="utf-8")
    (work / "keep.txt").write_text("base\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
    _git(work, "push", "-u", "origin", "main")

    _clone(origin, clone)

    # The commit the box is behind by — it bumps the very file that goes dirty.
    (work / "uv.lock").write_text('version = "0.4.11"\n', encoding="utf-8")
    (work / "added-upstream.txt").write_text("upstream\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "bump")
    _git(work, "push")

    return clone, work


class TestLosslessDirtIsDiscarded:
    def test_fast_forwards_past_a_locally_regenerated_file(self, repos: tuple[Path, Path]) -> None:
        clone, _ = repos
        # The recorded wedge: `uv run` re-locked uv.lock to exactly what upstream
        # already carries. A bare `pull --ff-only` aborts here.
        (clone / "uv.lock").write_text('version = "0.4.11"\n', encoding="utf-8")

        result = _run_script(clone)

        assert result.returncode == 0, result.stderr
        assert "uv.lock" in result.stdout
        assert _git(clone, "status", "--porcelain") == ""
        assert (clone / "uv.lock").read_text(encoding="utf-8") == 'version = "0.4.11"\n'
        assert (clone / "added-upstream.txt").exists()

    def test_fast_forwards_past_an_untracked_file_the_merge_would_create(self, repos: tuple[Path, Path]) -> None:
        clone, _ = repos
        # git refuses the merge for untracked collisions too, with a different message.
        (clone / "added-upstream.txt").write_text("upstream\n", encoding="utf-8")

        result = _run_script(clone)

        assert result.returncode == 0, result.stderr
        assert _git(clone, "status", "--porcelain") == ""

    def test_fast_forwards_past_a_locally_deleted_file(self, repos: tuple[Path, Path]) -> None:
        clone, _ = repos
        # A deletion holds no content, so restoring it can lose nothing.
        (clone / "uv.lock").unlink()

        result = _run_script(clone)

        assert result.returncode == 0, result.stderr
        assert (clone / "uv.lock").read_text(encoding="utf-8") == 'version = "0.4.11"\n'


class TestUniqueWorkIsNeverDestroyed:
    def test_unique_edit_to_an_incoming_file_fails_loud_and_is_kept(self, repos: tuple[Path, Path]) -> None:
        clone, _ = repos
        (clone / "uv.lock").write_text("hand-written local work\n", encoding="utf-8")

        result = _run_script(clone)

        assert result.returncode == 1
        assert "FATAL" in result.stderr
        assert "uv.lock" in result.stderr, "the diagnostic must NAME the blocking file"
        assert "checkout HEAD --" in result.stderr, "and give the recovery command"
        assert (clone / "uv.lock").read_text(encoding="utf-8") == "hand-written local work\n"

    def test_unique_untracked_file_colliding_with_the_merge_is_kept(self, repos: tuple[Path, Path]) -> None:
        clone, _ = repos
        (clone / "added-upstream.txt").write_text("different local content\n", encoding="utf-8")

        result = _run_script(clone)

        assert result.returncode == 1
        assert "added-upstream.txt" in result.stderr
        assert (clone / "added-upstream.txt").read_text(encoding="utf-8") == "different local content\n"

    def test_unrelated_local_edit_survives_the_fast_forward(self, repos: tuple[Path, Path]) -> None:
        clone, _ = repos
        # Dirt the merge does not touch never blocked the pull, and must not be
        # collected as collateral by the reconciliation either.
        (clone / "keep.txt").write_text("local work in progress\n", encoding="utf-8")

        result = _run_script(clone)

        assert result.returncode == 0, result.stderr
        assert (clone / "keep.txt").read_text(encoding="utf-8") == "local work in progress\n"
        assert (clone / "uv.lock").read_text(encoding="utf-8") == 'version = "0.4.11"\n'


class TestControl:
    def test_a_bare_ff_pull_really_does_abort_on_the_regenerated_file(self, repos: tuple[Path, Path]) -> None:
        # Control for the whole suite: prove the wedge exists, so a green above is
        # the guard working rather than a merge that never needed guarding.
        clone, _ = repos
        (clone / "uv.lock").write_text('version = "0.4.11"\n', encoding="utf-8")

        pull = subprocess.run(
            [_GIT, "-C", str(clone), "pull", "--ff-only"],
            check=False,
            capture_output=True,
            text=True,
            env=git_identity_env(),
        )

        assert pull.returncode != 0
        assert "would be overwritten by merge" in pull.stderr

    def test_clean_checkout_fast_forwards_unchanged(self, repos: tuple[Path, Path]) -> None:
        clone, _ = repos

        result = _run_script(clone)

        assert result.returncode == 0, result.stderr
        assert (clone / "uv.lock").read_text(encoding="utf-8") == 'version = "0.4.11"\n'


class TestDeployScriptUsesTheGuard:
    def test_deploy_sh_delegates_the_fast_forward_to_the_helper(self) -> None:
        body = (_ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
        assert "fast-forward-checkout.sh" in body, (
            "deploy.sh must fast-forward through the guard, not a bare `git pull --ff-only` "
            "that aborts on any stray lockfile write and reports nothing."
        )

    def test_the_helper_never_hard_resets_or_cleans(self) -> None:
        # The unwedge must stay content-equivalence-scoped: a blanket reset/clean
        # would also unwedge every case, and destroy uncommitted work in a clone
        # that host agents share.
        code = "\n".join(
            line for line in _SCRIPT.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
        )
        assert "reset --hard" not in code
        assert "git clean" not in code
