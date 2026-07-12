"""The scheduled durations-refresh job must open ONE mergeable refresh PR (#3160).

Two residual reliability bugs are pinned here against the workflow YAML.

CI-3: the refresh branch name must be STABLE (reused + force-updated), so an unmerged
refresh is updated in place instead of stacking a new dated PR every day.

CI-2: the refresh PR must be created/pushed with a token that TRIGGERS the required
``test (3.13)`` check (a PAT, ``TEATREE_GH_TOKEN``) — the default ``GITHUB_TOKEN`` never
fires it, so such a PR could never merge unaided. The step also fails LOUD when that
token is unset rather than silently opening an un-mergeable PR.
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
