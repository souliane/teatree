"""The scheduled durations-refresh job must open ONE mergeable refresh PR (#3160).

Two residual reliability bugs are pinned here against the workflow YAML.

CI-3: the refresh branch name must be STABLE (reused + force-updated), so an unmerged
refresh is updated in place instead of stacking a new dated PR every day.

CI-2: the refresh PR must be created/pushed with a token that TRIGGERS the required
``test (3.13)`` check (a PAT, ``TEATREE_GH_TOKEN``) — the default ``GITHUB_TOKEN`` never
fires it, so such a PR could never merge unaided. The step also fails LOUD when that
token is unset rather than silently opening an un-mergeable PR.

#4483: the branch is force-pushed BEFORE the PR is opened, so anything that exits
non-zero between the two leaves a refreshed commit with no PR and nothing to review.
``gh pr create --label <name>`` does exactly that when the label is absent from the
repo (measured on run 32864127275: ``could not add label: 'ci' not found`` sank the
step after all twelve shard artifacts had merged cleanly). Labelling is decoration;
it must never gate the deliverable.
"""

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _refresh_job() -> dict[str, Any]:
    jobs = cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"])
    return cast("dict[str, Any]", jobs["refresh-durations"])


def _steps() -> list[dict[str, Any]]:
    return [s for s in _refresh_job().get("steps", []) if isinstance(s, dict)]


def _pr_step() -> dict[str, Any]:
    matches = [s for s in _steps() if "Open or update" in str(s.get("name", ""))]
    assert matches, "refresh-durations must have an 'Open or update ... refresh PR' step."
    return matches[0]


def _checkout_step() -> dict[str, Any]:
    matches = [s for s in _steps() if "actions/checkout" in str(s.get("uses", ""))]
    assert matches, "refresh-durations must have an actions/checkout step."
    return matches[0]


def _pr_step_commands() -> str:
    """The step's shell minus comment lines, so prose can neither satisfy nor trip an assertion."""
    lines = str(_pr_step().get("run", "")).splitlines()
    return "\n".join(line for line in lines if not line.strip().startswith("#"))


def _gh_pr_create_invocation() -> str:
    """The ``gh pr create`` command with its backslash continuations."""
    lines = _pr_step_commands().splitlines()
    starts = [i for i, line in enumerate(lines) if "gh pr create" in line]
    assert starts, "refresh-durations must still open the PR with `gh pr create`."
    collected = [lines[starts[0]]]
    index = starts[0]
    while collected[-1].rstrip().endswith("\\"):
        index += 1
        collected.append(lines[index])
    return "\n".join(collected)


class TestRefreshBranchIsStable:
    """CI-3: a STABLE branch name means at most one open refresh PR, updated in place."""

    def test_branch_name_is_not_dated(self) -> None:
        run = str(_pr_step().get("run", ""))
        assert 'BRANCH="ci/test-durations-refresh"' in run, (
            "The refresh branch must be the STABLE name ci/test-durations-refresh so an "
            "unmerged refresh is force-updated in place, not stacked as a new PR each day."
        )

    def test_branch_does_not_embed_the_date(self) -> None:
        run = str(_pr_step().get("run", ""))
        branch_lines = [line for line in run.splitlines() if line.strip().startswith("BRANCH=")]
        assert branch_lines, "The PR step must assign a BRANCH variable."
        for line in branch_lines:
            assert "$(date" not in line, (
                "The refresh branch name must NOT embed the date — a dated branch opens a "
                "NEW PR every unmerged day, stacking conflicting PRs (CI-3)."
            )


class TestRefreshPrTriggersCi:
    """CI-2: the refresh PR must be opened with a token that fires the required check."""

    def test_pr_step_uses_the_pat_not_github_token(self) -> None:
        gh_token = str(_pr_step().get("env", {}).get("GH_TOKEN", ""))
        assert "TEATREE_GH_TOKEN" in gh_token, (
            "The refresh-PR step must use TEATREE_GH_TOKEN so the PR triggers the required "
            "test (3.13) check; the default GITHUB_TOKEN never fires it (un-mergeable PR)."
        )
        assert "github.token" not in gh_token, (
            "GH_TOKEN must NOT be the default github.token — a PR/push it authenticates never "
            "triggers downstream workflows, so the required check never runs (CI-2)."
        )

    def test_checkout_persists_the_pat_for_push(self) -> None:
        token = str(_checkout_step().get("with", {}).get("token", ""))
        assert "TEATREE_GH_TOKEN" in token, (
            "The checkout must persist TEATREE_GH_TOKEN so the `git push` to the refresh "
            "branch is attributed to a real identity and re-triggers CI on updates (CI-2)."
        )

    def test_pr_step_fails_loud_when_token_unset(self) -> None:
        run = str(_pr_step().get("run", ""))
        guard_msg = (
            "The refresh-PR step must fail LOUD when the CI-triggering token is unset, rather "
            "than silently opening a PR whose required check never fires (CI-2)."
        )
        assert 'if [ -z "${GH_TOKEN:-}" ]' in run, guard_msg
        assert "exit 1" in run, guard_msg


class TestLabellingNeverGatesTheRefreshPr:
    """#4483: a missing label must not sink the refresh after the branch is pushed."""

    def test_pr_create_does_not_pass_label(self) -> None:
        assert "--label" not in _gh_pr_create_invocation(), (
            "`gh pr create --label <name>` exits 1 when the label is absent from the repo, and "
            "the step runs under `set -e` AFTER the refresh branch is already force-pushed — so "
            "a renamed or missing label strands the refreshed commit with no PR (#4483). Apply "
            "the label in its own non-fatal call instead."
        )

    def test_label_is_applied_non_fatally(self) -> None:
        labelling = [line for line in _pr_step_commands().splitlines() if "--add-label" in line]
        assert labelling, (
            "The refresh PR should still be labelled — apply it with `gh pr edit --add-label` "
            "after creation, so the label is decoration rather than a gate (#4483)."
        )
        assert all("||" in line for line in labelling), (
            f"Every `--add-label` call must be non-fatal (`|| true`), else it re-introduces the "
            f"#4483 failure under `set -e`. Found: {labelling!r}"
        )
