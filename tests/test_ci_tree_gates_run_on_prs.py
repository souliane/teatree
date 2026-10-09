"""The full-tree leak gates run on pull_request, not only push/schedule (#44).

``overlay-leak-tree`` and ``banned-terms-tree`` scan the whole committed tree
for overlay-scoped names, opaque Slack/forge IDs, and brand terms. They used
to run only on push-to-main and on the daily schedule, so a leak introduced by
a PR passed every PR check and turned main RED on merge (the #2801
hardcoded-handle incident, hotfixed by #2804). These tests pin that the gates
ALSO run on pull_request — catching the leak pre-merge — while keeping the
push/schedule backstop. Fork PRs cannot read the registry secret, so each job
reports the explicit skip while same-repo PRs run the full classed scan.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_TREE_GATES = ("overlay-leak-tree", "banned-terms-tree")


def _jobs() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"])


def _steps(job_name: str) -> list[dict[str, Any]]:
    return [s for s in _jobs()[job_name]["steps"] if isinstance(s, dict)]


def _run_where(job_name: str, keep: Callable[[str], bool]) -> str:
    return " ".join(str(s.get("run", "")) for s in _steps(job_name) if keep(str(s.get("if", ""))))


def _pr_step_env(job_name: str) -> dict[str, Any]:
    env: dict[str, Any] = {}
    for step in _steps(job_name):
        if "== 'pull_request'" in str(step.get("if", "")):
            env.update(cast("dict[str, Any]", step.get("env", {})))
    return env


class TestTreeGatesTriggerOnPullRequest:
    def test_both_tree_gates_run_on_push_schedule_and_pr(self) -> None:
        jobs = _jobs()
        for lane in _TREE_GATES:
            condition = str(jobs[lane].get("if", ""))
            assert "push" in condition, f"{lane} must keep its push trigger"
            assert "schedule" in condition, f"{lane} must keep its schedule trigger"
            assert "pull_request" in condition, f"{lane} must ALSO run on pull_request (#44)"


class TestPrSideRunSecretRouting:
    def test_overlay_leak_pr_and_main_use_the_same_required_scan(self) -> None:
        pr = _run_where("overlay-leak-tree", lambda c: "== 'pull_request'" in c)
        non_pr = _run_where("overlay-leak-tree", lambda c: "!= 'pull_request'" in c)
        assert "check_no_overlay_leak.py" in pr, "the PR step must run the full-tree overlay-leak scan"
        assert "check_no_overlay_leak.py" in non_pr
        assert "--require-terms" not in pr + non_pr
        assert "TEATREE_TERM_REGISTRY" in _pr_step_env("overlay-leak-tree"), (
            "the PR step threads the term secret so same-repo PRs get full term coverage"
        )

    def test_banned_terms_pr_step_uses_the_same_required_scan_as_main(self) -> None:
        pr = _run_where("banned-terms-tree", lambda c: "== 'pull_request'" in c)
        non_pr = _run_where("banned-terms-tree", lambda c: "!= 'pull_request'" in c)
        assert "scan-tree" in pr, "the PR step must run the full-tree banned-terms scan"
        assert "--require-brands" not in pr
        assert "--require-brands" not in non_pr
        assert "TEATREE_TERM_REGISTRY" in _pr_step_env("banned-terms-tree"), (
            "the PR step threads the brand secret so same-repo PRs get full brand coverage"
        )

    def test_banned_terms_pr_step_has_no_unset_fallback(self) -> None:
        pr = _run_where("banned-terms-tree", lambda c: "== 'pull_request'" in c)
        assert "--allow-unset" not in pr
        assert "T3_BANNED_TERMS_CONFIG" not in _pr_step_env("banned-terms-tree"), (
            "the dead T3_BANNED_TERMS_CONFIG file fallback must be gone"
        )

    def test_tree_gates_thread_the_consolidated_registry_secret(self) -> None:
        for keep in (lambda c: "== 'pull_request'" in c, lambda c: "!= 'pull_request'" in c):
            env: dict[str, object] = {}
            for step in _steps("banned-terms-tree"):
                if keep(str(step.get("if", ""))):
                    env.update(step.get("env", {}))
            assert "TEATREE_TERM_REGISTRY" in env, "each tree-scan step must thread the consolidated registry secret"


class TestForkTermRegistryRouting:
    def test_every_registry_job_has_a_visible_fork_skip_and_same_repo_scan(self) -> None:
        scan_commands = {
            "banned-terms-tree": "banned-terms scan-tree",
            "overlay-leak-tree": "check_no_overlay_leak.py",
            "term-source-drift": "term_source_drift.py check-ci",
        }
        message = "skipped: the term registry secret is not available to pull requests from forks"
        for job_name, command in scan_commands.items():
            steps = _steps(job_name)
            pr_scans = [
                step
                for step in steps
                if command in str(step.get("run", ""))
                and "github.event_name == 'pull_request'" in str(step.get("if", ""))
            ]
            assert len(pr_scans) == 1, job_name
            assert "github.event.pull_request.head.repo.full_name == github.repository" in pr_scans[0]["if"]
            assert pr_scans[0]["env"] == {"TEATREE_TERM_REGISTRY": "${{ secrets.TEATREE_TERM_REGISTRY }}"}
            fork_steps = [step for step in steps if message in str(step.get("run", ""))]
            assert len(fork_steps) == 1, job_name
            assert "github.event.pull_request.head.repo.full_name != github.repository" in fork_steps[0]["if"]
            for step in steps:
                if step is fork_steps[0]:
                    continue
                condition = str(step.get("if", ""))
                assert "github.event.pull_request.head.repo.full_name == github.repository" in condition or (
                    "github.event_name != 'pull_request'" in condition
                ), f"{job_name}: a fork PR must run only its visible skip step"

    def test_fork_lint_runs_every_other_hook_and_reports_term_scan_skip(self) -> None:
        steps = _steps("lint")
        prek = next(step for step in steps if "prek run --all-files" in str(step.get("run", "")))
        skip = str(prek["env"]["SKIP"])
        assert "banned-terms,no-overlay-leak" in skip
        assert "github.event.pull_request.head.repo.full_name != github.repository" in skip
        assert any(
            "skipped: the term registry secret is not available to pull requests from forks" in str(step.get("run", ""))
            for step in steps
        )

    def test_no_legacy_secret_or_unset_mode_in_workflow(self) -> None:
        workflow = _CI_WORKFLOW.read_text(encoding="utf-8")
        for legacy in ("TEATREE_BANNED_BRANDS", "TEATREE_BANNED_TERMS", "TEATREE_OVERLAY_LEAK_TERMS", "--allow-unset"):
            assert legacy not in workflow
