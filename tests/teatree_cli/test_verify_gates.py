"""``t3 tool verify-gates`` runs BOTH stages AND discloses the tree it measured.

Meta-test pinning two contracts. First the CI-parity one: a bare ``prek run
--all-files`` only fires the commit/manual-stage hooks (``default_stages:
[commit, manual]``), so the push-stage gates CI re-runs (comment-density,
doc-update, ensure-pr, the public-repo leak gate) are structurally skipped.

Second, the venue one (#4720): the command takes no target, so run from a main
clone it grades the default branch rather than the branch under review and still
exits 0 — a reviewer handing back that exit code reports a green for a tree
nobody asked about. Every run must name the sha it measured, refuse a clean main
clone on its default branch, honour ``--expect-sha``, and name the CI jobs no
local hook covers.
"""

import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.verify_gates import UNCOVERED_CI_JOBS, _resource_refusal
from teatree.quality.changed_set import ChangedSet, ChangeEntry
from tests._git_repo import make_git_repo, run_git

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_real_gate_receipt():
    with patch("teatree.cli.verify_gates.write_gate_receipt"):
        yield


def test_resource_preflight_measures_disk_and_memory() -> None:
    with (
        patch("teatree.cli.verify_gates.free_bytes", return_value=1024**3),
        patch("teatree.cli.verify_gates.read_ram_headroom") as memory,
    ):
        assert "disk" in _resource_refusal()
        memory.assert_not_called()
    with (
        patch("teatree.cli.verify_gates.free_bytes", return_value=8 * 1024**3),
        patch("teatree.cli.verify_gates.read_ram_headroom", return_value=SimpleNamespace(available_mib=900)),
    ):
        assert "memory" in _resource_refusal()
    with (
        patch("teatree.cli.verify_gates.free_bytes", return_value=8 * 1024**3),
        patch("teatree.cli.verify_gates.read_ram_headroom", return_value=SimpleNamespace(available_mib=8192)),
    ):
        assert _resource_refusal() == ""


def _calls(mock) -> list[list[str]]:
    return [list(call.args[0]) for call in mock.call_args_list]


def _changed(*paths: str) -> ChangedSet:
    return ChangedSet(tuple(ChangeEntry("M", path) for path in paths), "origin/main")


def _text(result) -> str:
    """Everything the run wrote — verify-gates reports on stderr."""
    try:
        return result.output + result.stderr
    except ValueError:
        return result.output


@pytest.fixture
def main_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clean primary clone on its default branch — the #4655 false-green venue."""
    clone = make_git_repo(tmp_path / "clone")
    monkeypatch.chdir(clone)
    return clone


@pytest.fixture
def worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A linked worktree on a feature branch — where a ticket is actually graded."""
    clone = make_git_repo(tmp_path / "clone")
    checkout = tmp_path / "wt"
    run_git(clone, "worktree", "add", "-b", "feature", str(checkout))
    (checkout / "README.md").write_text("test checkout\n", encoding="utf-8")
    run_git(checkout, "add", "README.md")
    run_git(checkout, "commit", "-q", "-m", "add README")
    monkeypatch.chdir(checkout)
    return checkout


def _head_sha(repo: Path) -> str:
    return run_git(repo, "rev-parse", "HEAD")


@pytest.fixture
def _healthy_resource_preflight() -> Iterator[None]:
    with patch("teatree.cli.verify_gates._resource_refusal", return_value=""):
        yield


class TestVerifyGatesRunsBothStages:
    @pytest.fixture(autouse=True)
    def _healthy_gate_host(self, worktree: Path) -> Iterator[None]:
        with (
            patch("teatree.cli.verify_gates._timeout_available", return_value=True),
            patch("teatree.cli.verify_gates._resource_refusal", return_value=""),
        ):
            yield

    def test_refuses_low_disk_before_launching_prek(self) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates._resource_refusal", return_value="disk: 1 GiB free below 4 GiB floor"),
            patch("teatree.cli.verify_gates.run_streamed") as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1
        assert "disk: 1 GiB free below 4 GiB floor" in result.output
        run.assert_not_called()

    def test_refuses_low_memory_before_launching_prek(self) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch(
                "teatree.cli.verify_gates._resource_refusal",
                return_value="memory: 900 MiB available below 2048 MiB floor",
            ),
            patch("teatree.cli.verify_gates.run_streamed") as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1
        assert "memory: 900 MiB available below 2048 MiB floor" in result.output
        run.assert_not_called()

    def test_invokes_push_stage_hooks_not_just_commit(self) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates._timeout_available", return_value=True),
            patch("teatree.cli.verify_gates.changed_paths", return_value=_changed("README.md")),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0
        calls = _calls(run)
        assert all(c[:4] == ["timeout", "--signal=TERM", "--kill-after=15s", "600s"] for c in calls)
        assert any(c[4:] == ["prek", "run", "--files", "README.md"] for c in calls)
        # One push-stage run — the gate CI re-runs that the bare run skips.
        push = [c for c in calls if "--hook-stage" in c]
        assert push, "verify-gates must invoke the push stage"
        assert push[0][-2:] == ["--hook-stage", "pre-push"]

    def test_config_change_falls_back_to_all_files(self) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates._timeout_available", return_value=True),
            patch("teatree.cli.verify_gates.changed_paths", return_value=_changed("pyproject.toml")),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0
        assert all("--all-files" in c for c in _calls(run))

    def test_timeout_is_incomplete_not_green(self) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates._timeout_available", return_value=True),
            patch("teatree.cli.verify_gates.changed_paths", return_value=_changed("README.md")),
            patch("teatree.cli.verify_gates.run_streamed", side_effect=[124, 0]),
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1
        assert "INCOMPLETE" in result.output

    def test_missing_timeout_fails_closed(self) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates._timeout_available", return_value=False),
            patch("teatree.cli.verify_gates.run_streamed") as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1
        run.assert_not_called()

    def test_uses_canonical_pre_push_stage_value(self) -> None:
        """Prek rejects the literal ``push``; the canonical value is ``pre-push``."""
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            runner.invoke(app, ["tool", "verify-gates"])
        push = [c for c in _calls(run) if "--hook-stage" in c]
        assert push, "verify-gates must invoke the push stage"
        assert "push" not in push[0], "must pass pre-push, not the rejected 'push'"

    def test_fails_when_push_stage_fails(self, worktree: Path) -> None:
        def _rc(cmd: list[str], **_kwargs: object) -> int:
            return 1 if "--hook-stage" in cmd else 0

        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", side_effect=_rc),
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1

    def test_fails_when_commit_stage_fails(self, worktree: Path) -> None:
        def _rc(cmd: list[str], **_kwargs: object) -> int:
            return 0 if "--hook-stage" in cmd else 1

        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", side_effect=_rc),
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1

    def test_both_green_passes(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0),
            patch("teatree.cli.verify_gates.write_gate_receipt") as receipt,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0
        assert receipt.call_args_list[0].kwargs["state"] == "incomplete"
        assert receipt.call_args_list[-1].kwargs["state"] == "green"

    def test_missing_prek_fails_closed(self, worktree: Path) -> None:
        with patch("teatree.cli.verify_gates._prek_available", return_value=False):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1


@pytest.mark.usefixtures("_healthy_resource_preflight")
class TestVerifyGatesDisclosesTheTree:
    def test_new_head_committed_during_gates_cannot_get_green_receipt(self, worktree: Path) -> None:
        starting_head = _head_sha(worktree)

        def _run(cmd: list[str], **_kwargs: object) -> int:
            if "--hook-stage" not in cmd:
                run_git(worktree, "commit", "-q", "--allow-empty", "-m", "concurrent commit")
            return 0

        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", side_effect=_run),
            patch("teatree.cli.verify_gates.write_gate_receipt") as receipt,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])

        assert _head_sha(worktree) != starting_head
        assert result.exit_code == 2
        assert "checkout changed" in _text(result)
        assert receipt.call_args_list[-1].kwargs["state"] == "incomplete"

    def test_green_summary_names_the_measured_head_sha(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0),
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0
        assert _head_sha(worktree) in _text(result)

    def test_failed_summary_names_the_measured_head_sha(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=1),
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1
        assert _head_sha(worktree) in _text(result)

    def test_banner_names_the_branch_and_venue(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0),
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        text = _text(result)
        assert "feature" in text
        assert "worktree" in text

    def test_green_run_names_the_ci_jobs_it_does_not_cover(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0),
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        text = _text(result)
        for job in UNCOVERED_CI_JOBS:
            assert job in text, f"the uncovered-surfaces line must name {job}"

    def test_uncovered_jobs_are_real_ci_jobs(self) -> None:
        """A stale job name would advertise coverage nobody can check."""
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        jobs = set(re.findall(r"^  ([a-z0-9][a-z0-9-]*):$", ci, flags=re.MULTILINE))
        assert jobs, "could not parse any job key out of ci.yml"
        assert set(UNCOVERED_CI_JOBS) <= jobs, f"not CI jobs: {set(UNCOVERED_CI_JOBS) - jobs}"


@pytest.mark.usefixtures("_healthy_resource_preflight")
class TestVerifyGatesRefusesTheWrongTree:
    def test_clean_main_clone_on_default_branch_is_refused(self, main_clone: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
            patch("teatree.cli.verify_gates.write_gate_receipt") as receipt,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 2
        assert not _calls(run), "a refused tree must not be graded"
        receipt.assert_not_called()
        assert "--allow-main-clone" in _text(result)

    def test_allow_main_clone_grades_it_anyway(self, main_clone: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates", "--allow-main-clone"])
        assert result.exit_code == 0
        assert _calls(run), "the explicit override must still run the gates"

    def test_dirty_main_clone_runs_because_it_carries_a_change(self, main_clone: Path) -> None:
        (main_clone / "scratch.txt").write_text("work in progress\n", encoding="utf-8")
        run_git(main_clone, "add", "scratch.txt")
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0
        assert _calls(run)

    def test_detached_head_is_never_the_default_branch(self, main_clone: Path) -> None:
        run_git(main_clone, "checkout", "--detach")
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0
        assert _calls(run), "a cold-review detached checkout is a real target"

    def test_untracked_scratch_does_not_excuse_a_clean_main_clone(self, main_clone: Path) -> None:
        """``--all-files`` grades TRACKED files, so an unadded scratch file measured nothing extra."""
        (main_clone / "scratch.txt").write_text("notes\n", encoding="utf-8")
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 2
        assert not _calls(run)

    def test_configured_target_branch_is_refused_like_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fork's ``teatree.targetBranch`` checkout is the same structural non-target."""
        clone = make_git_repo(tmp_path / "fork", default_branch="development")
        run_git(clone, "config", "teatree.targetBranch", "development")
        monkeypatch.chdir(clone)
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 2
        assert not _calls(run)

    def test_non_git_directory_is_refused_before_any_hook(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 2
        assert not _calls(run)


@pytest.mark.usefixtures("_healthy_resource_preflight")
class TestVerifyGatesExpectSha:
    def test_mismatched_target_is_refused_before_any_hook(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates", "--expect-sha", "0" * 40])
        assert result.exit_code == 2
        assert not _calls(run), "the wrong tree must not be graded"
        assert _head_sha(worktree) in _text(result)

    def test_matching_full_sha_runs_both_stages(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates", "--expect-sha", _head_sha(worktree)])
        assert result.exit_code == 0
        assert len(_calls(run)) == 2

    def test_matching_abbreviated_sha_runs(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0),
        ):
            result = runner.invoke(app, ["tool", "verify-gates", "--expect-sha", _head_sha(worktree)[:12]])
        assert result.exit_code == 0

    def test_uppercase_sha_from_a_forge_ui_still_matches(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0),
        ):
            result = runner.invoke(app, ["tool", "verify-gates", "--expect-sha", _head_sha(worktree).upper()])
        assert result.exit_code == 0

    def test_explicit_target_overrides_the_main_clone_refusal(self, main_clone: Path) -> None:
        """Naming the sha IS saying which tree you meant — the refusal has nothing to add."""
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates", "--expect-sha", _head_sha(main_clone)])
        assert result.exit_code == 0
        assert _calls(run)

    def test_env_var_supplies_the_target(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(
                app,
                ["tool", "verify-gates"],
                env={"T3_VERIFY_GATES_EXPECT_SHA": "0" * 40},
            )
        assert result.exit_code == 2
        assert not _calls(run)

    def test_empty_env_var_is_not_a_target(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"], env={"T3_VERIFY_GATES_EXPECT_SHA": ""})
        assert result.exit_code == 0
        assert _calls(run)
