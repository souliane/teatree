"""The full-tree leak gates run on pull_request, not only push/schedule (#44).

``overlay-leak-tree`` and ``banned-terms-tree`` scan the whole committed tree
for overlay-scoped names, opaque Slack/forge IDs, and brand terms. They used
to run only on push-to-main and on the daily schedule, so a leak introduced by
a PR passed every PR check and turned main RED on merge (the #2801
hardcoded-handle incident, hotfixed by #2804). These tests pin that the gates
ALSO run on pull_request — catching the leak pre-merge — while keeping the
push/schedule backstop and never false-redding a fork PR that cannot read the
term/brand secret.
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


class TestPrSideRunIsSecretOptional:
    def test_overlay_leak_pr_step_drops_require_terms_main_keeps_it(self) -> None:
        pr = _run_where("overlay-leak-tree", lambda c: "== 'pull_request'" in c)
        non_pr = _run_where("overlay-leak-tree", lambda c: "!= 'pull_request'" in c)
        assert "check_no_overlay_leak.py" in pr, "the PR step must run the full-tree overlay-leak scan"
        assert "--require-terms" not in pr, "the PR step must NOT require terms (a fork PR has no secret)"
        assert "--require-terms" in non_pr, "the push/schedule step keeps --require-terms (loud-on-misconfig)"
        assert "TEATREE_OVERLAY_LEAK_TERMS" in _pr_step_env("overlay-leak-tree"), (
            "the PR step threads the term secret so same-repo PRs get full term coverage"
        )

    def test_banned_terms_pr_step_drops_require_brands_main_keeps_it(self) -> None:
        pr = _run_where("banned-terms-tree", lambda c: "== 'pull_request'" in c)
        non_pr = _run_where("banned-terms-tree", lambda c: "!= 'pull_request'" in c)
        assert "scan-tree" in pr, "the PR step must run the full-tree banned-terms scan"
        assert "--require-brands" not in pr, "the PR step must NOT require brands (a fork PR has no secret)"
        assert "--require-brands" in non_pr, "the push/schedule step keeps --require-brands (loud-on-misconfig)"
        assert "TEATREE_BANNED_BRANDS" in _pr_step_env("banned-terms-tree"), (
            "the PR step threads the brand secret so same-repo PRs get full brand coverage"
        )

    def test_banned_terms_pr_step_has_explicit_allow_unset_fork_fallback(self) -> None:
        # A fork PR cannot read $TEATREE_BANNED_BRANDS; the loader would refuse a
        # genuinely-unset list (exit 2). The PR step opts in EXPLICITLY via
        # --allow-unset so the always-on terminology pass runs pre-merge — the
        # fail-closed-by-default flag that replaced the dead T3_BANNED_TERMS_CONFIG
        # file fallback (no code ever consumed it).
        pr = _run_where("banned-terms-tree", lambda c: "== 'pull_request'" in c)
        assert "--allow-unset" in pr, "the PR step must opt in to the terminology-only pass via --allow-unset"
        assert "T3_BANNED_TERMS_CONFIG" not in _pr_step_env("banned-terms-tree"), (
            "the dead T3_BANNED_TERMS_CONFIG file fallback must be gone"
        )

    def test_tree_gates_thread_the_consolidated_registry_secret(self) -> None:
        # Dual-env transition: the banned-terms-tree job threads the consolidated
        # TEATREE_TERM_REGISTRY secret alongside the legacy brand secret on every
        # step, so the registry activates on cutover with no CI edit.
        for keep in (lambda c: "== 'pull_request'" in c, lambda c: "!= 'pull_request'" in c):
            env: dict[str, object] = {}
            for step in _steps("banned-terms-tree"):
                if keep(str(step.get("if", ""))):
                    env.update(step.get("env", {}))
            assert "TEATREE_TERM_REGISTRY" in env, "each tree-scan step must thread the consolidated registry secret"
