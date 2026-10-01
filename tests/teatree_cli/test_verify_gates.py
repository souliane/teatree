"""``t3 tool verify-gates`` runs every CI-gated stage AND discloses the tree it measured.

Meta-test pinning two contracts. First the CI-parity one: a bare ``prek run
--all-files`` only fires the commit-stage hooks, so the push-stage gates CI
re-runs (comment-density, doc-update, ensure-pr, the public-repo leak gate) and
the manual-stage hooks CI runs as jobs (test-path-mirror) are structurally skipped.

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
import yaml
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.verify_gates import CI_JOB_MANUAL_HOOKS, UNCOVERED_CI_JOBS, _declared_manual_hooks, _resource_refusal
from teatree.quality.changed_set import ChangedSet, ChangeEntry
from tests._git_repo import make_git_repo, run_git

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_real_gate_receipt():
    with patch("teatree.cli.verify_gates.write_gate_receipt"):
        yield


def test_resource_preflight_measures_disk_and_memory(tmp_path: Path) -> None:
    with (
        patch("teatree.cli.verify_gates.free_bytes", return_value=1024**3),
        patch("teatree.cli.verify_gates.read_ram_headroom") as memory,
    ):
        assert "disk" in _resource_refusal(tmp_path)
        memory.assert_not_called()
    with (
        patch("teatree.cli.verify_gates.free_bytes", return_value=8 * 1024**3),
        patch("teatree.cli.verify_gates.read_ram_headroom", return_value=SimpleNamespace(available_mib=900)),
    ):
        assert "memory" in _resource_refusal(tmp_path)
    with (
        patch("teatree.cli.verify_gates.free_bytes", return_value=8 * 1024**3),
        patch("teatree.cli.verify_gates.read_ram_headroom", return_value=SimpleNamespace(available_mib=8192)),
    ):
        assert _resource_refusal(tmp_path) == ""


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


def _make_feature_worktree(tmp_path: Path, name: str) -> Path:
    """A linked worktree on a feature branch, independent of the process cwd."""
    clone = make_git_repo(tmp_path / f"{name}-clone")
    checkout = tmp_path / name
    run_git(clone, "worktree", "add", "-b", f"{name}-feature", str(checkout))
    (checkout / "README.md").write_text("test checkout\n", encoding="utf-8")
    run_git(checkout, "add", "README.md")
    run_git(checkout, "commit", "-q", "-m", "add README")
    return checkout


def _write_prek_config(repo: Path, *hooks: dict[str, object], **top_level: object) -> None:
    config = {**top_level, "repos": [{"repo": "local", "hooks": list(hooks)}]}
    (repo / ".pre-commit-config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def _hook(hook_id: str, **fields: object) -> dict[str, object]:
    return {"id": hook_id, "name": hook_id, "entry": "true", "language": "system", **fields}


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

    def test_invokes_the_manual_stage_hooks_ci_gates_on(self) -> None:
        """CI's test-path-mirror job runs a ``stages: [manual]`` hook no commit/push run fires."""
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates._timeout_available", return_value=True),
            patch("teatree.cli.verify_gates.changed_paths", return_value=_changed("README.md")),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0
        manual = [c for c in _calls(run) if c[-2:] == ["--hook-stage", "manual"]]
        assert len(manual) == 1, "verify-gates must invoke the manual stage once"
        assert "test-path-mirror" in manual[0]

    def test_repo_declaring_no_ci_job_hook_skips_the_manual_stage(self, worktree: Path) -> None:
        """A selector naming no declared hook makes prek exit 1, so the stage must not run at all."""
        _write_prek_config(worktree, _hook("ruff"))
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0, _text(result)
        assert len(_calls(run)) == 2
        assert not [c for c in _calls(run) if "manual" in c], "no hook declared, so no manual-stage run"
        assert "manual stage skipped" in _text(result)
        summaries = [line for line in _text(result).splitlines() if "all gate stages green" in line]
        assert summaries
        assert all("manual" not in line for line in summaries), "a green summary must not claim a stage never run"

    def test_manual_stage_selects_only_the_declared_ci_job_hooks(self, worktree: Path) -> None:
        _write_prek_config(worktree, _hook("test-path-mirror", stages=["manual"]))
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0, _text(result)
        (manual,) = [c for c in _calls(run) if c[-2:] == ["--hook-stage", "manual"]]
        assert "test-path-mirror" in manual
        assert "test-shape" not in manual

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
            patch("teatree.cli.verify_gates.run_streamed", side_effect=[124, 0, 0]),
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

    @pytest.mark.parametrize("failing_stage", ["commit", "pre-push", "manual"])
    def test_fails_when_any_stage_fails(self, worktree: Path, failing_stage: str) -> None:
        def _rc(cmd: list[str], **_kwargs: object) -> int:
            stage = cmd[-1] if "--hook-stage" in cmd else "commit"
            return 1 if stage == failing_stage else 0

        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", side_effect=_rc),
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1
        assert f"FAILED stage(s): {failing_stage}" in _text(result)

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


class TestDeclaredManualHooks:
    """Which CI-job hooks the repo's prek config runs at the manual stage."""

    def test_missing_config_fails_safe_to_every_hook(self, tmp_path: Path) -> None:
        assert _declared_manual_hooks(tmp_path) == CI_JOB_MANUAL_HOOKS

    @pytest.mark.parametrize(
        "raw",
        [
            b"repos: [\n",
            b"\xff\xfe\x00",
            b"",
            b"- a list\n",
            b"repos: 3\n",
            b"repos: [3]\n",
            b"repos: [{hooks: [{}]}]\n",
        ],
        ids=["yaml-error", "not-utf8", "empty", "top-level-list", "repos-not-list", "repo-not-mapping", "hook-no-id"],
    )
    def test_unreadable_config_fails_safe_to_every_hook(self, tmp_path: Path, raw: bytes) -> None:
        (tmp_path / ".pre-commit-config.yaml").write_bytes(raw)
        assert _declared_manual_hooks(tmp_path) == CI_JOB_MANUAL_HOOKS

    def test_neither_hook_declared_selects_none(self, tmp_path: Path) -> None:
        _write_prek_config(tmp_path, _hook("ruff"))
        assert _declared_manual_hooks(tmp_path) == ()

    def test_hook_stages_decide(self, tmp_path: Path) -> None:
        _write_prek_config(
            tmp_path,
            _hook("test-path-mirror", stages=["manual"]),
            _hook("test-shape", stages=["pre-commit"]),
            default_stages=["manual"],
        )
        assert _declared_manual_hooks(tmp_path) == ("test-path-mirror",)

    def test_hook_without_stages_takes_the_default_stages(self, tmp_path: Path) -> None:
        _write_prek_config(tmp_path, _hook("test-path-mirror"), _hook("test-shape"), default_stages=["pre-commit"])
        assert _declared_manual_hooks(tmp_path) == ()

    def test_hook_without_any_stages_runs_at_every_stage(self, tmp_path: Path) -> None:
        _write_prek_config(tmp_path, _hook("test-shape"))
        assert _declared_manual_hooks(tmp_path) == ("test-shape",)

    def test_this_repo_declares_both(self) -> None:
        assert _declared_manual_hooks(REPO_ROOT) == CI_JOB_MANUAL_HOOKS


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

    def test_every_manual_only_ci_job_is_run_or_disclosed(self) -> None:
        """A manual-stage hook CI runs as a job must be run here or named as uncovered."""
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        jobs = set(re.findall(r"^  ([a-z0-9][a-z0-9-]*):$", ci, flags=re.MULTILINE))
        config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
        manual = {hook["id"] for repo in config["repos"] for hook in repo["hooks"] if hook.get("stages") == ["manual"]}
        manual_jobs = manual & jobs
        assert set(CI_JOB_MANUAL_HOOKS) <= manual_jobs, (
            f"not manual CI-job hooks: {set(CI_JOB_MANUAL_HOOKS) - manual_jobs}"
        )
        unaccounted = manual_jobs - set(CI_JOB_MANUAL_HOOKS) - set(UNCOVERED_CI_JOBS)
        assert not unaccounted, f"manual-stage CI jobs neither run nor disclosed: {unaccounted}"


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

    def test_matching_full_sha_runs_every_stage(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates", "--expect-sha", _head_sha(worktree)])
        assert result.exit_code == 0
        assert len(_calls(run)) == 3

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


class TestVerifyGatesFollowsTheInvocationCwd:
    """#4859: the containerized process cwd is the container WORKDIR, not the operator's.

    Measuring/grading ``Path.cwd()`` graded whatever checkout happened to be mounted at
    WORKDIR (or refused a non-git WORKDIR) instead of the ticket worktree declared via
    ``TEATREE_INVOCATION_CWD``.
    """

    @pytest.fixture(autouse=True)
    def _healthy_gate_host(self) -> Iterator[None]:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates._timeout_available", return_value=True),
            patch("teatree.cli.verify_gates._resource_refusal", return_value=""),
        ):
            yield

    def test_declared_invocation_cwd_is_measured_not_the_process_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        declared = _make_feature_worktree(tmp_path, "declared")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.setenv("TEATREE_INVOCATION_CWD", str(declared))
        monkeypatch.chdir(elsewhere)

        with (
            patch("teatree.cli.verify_gates.run_streamed", return_value=0),
            patch("teatree.cli.verify_gates.write_gate_receipt") as receipt,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])

        assert result.exit_code == 0, _text(result)
        assert _head_sha(declared) in _text(result)
        assert receipt.call_args_list[-1].args[0] == declared

    def test_prek_subprocess_runs_with_cwd_the_declared_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        declared = _make_feature_worktree(tmp_path, "declared")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.setenv("TEATREE_INVOCATION_CWD", str(declared))
        monkeypatch.chdir(elsewhere)

        with patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run:
            result = runner.invoke(app, ["tool", "verify-gates"])

        assert result.exit_code == 0, _text(result)
        assert run.call_args_list, "prek must have been invoked"
        assert all(call.kwargs.get("cwd") == declared for call in run.call_args_list)

    def test_an_explicit_repo_still_wins_over_the_declared_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: the declaration is a DEFAULT, never an override of what the caller asked."""
        declared = _make_feature_worktree(tmp_path, "declared")
        explicit = _make_feature_worktree(tmp_path, "explicit")
        monkeypatch.setenv("TEATREE_INVOCATION_CWD", str(declared))

        with patch("teatree.cli.verify_gates.run_streamed", return_value=0):
            result = runner.invoke(app, ["tool", "verify-gates", "--repo", str(explicit)])

        assert result.exit_code == 0, _text(result)
        assert _head_sha(explicit) in _text(result)

    def test_no_declaration_falls_back_to_the_process_cwd(
        self, worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: a host-native run is byte-identical to the pre-#4859 behaviour."""
        monkeypatch.delenv("TEATREE_INVOCATION_CWD", raising=False)

        with patch("teatree.cli.verify_gates.run_streamed", return_value=0):
            result = runner.invoke(app, ["tool", "verify-gates"])

        assert result.exit_code == 0, _text(result)
        assert _head_sha(worktree) in _text(result)
