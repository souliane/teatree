"""Guard: the CI concurrency block supersede-cancels PR waves, never main.

Move (A) of the CI-runtime fix: a re-push to a PR must cancel its OWN superseded
in-progress wave (killing the self-inflicted queue stacking), while push-to-main
and the schedule must NEVER be cancelled — their full-tree banned-terms /
overlay-leak backstops must run to completion. This pins both halves so a future
edit cannot drop the cancel (queue stacking returns) or extend the cancel to main
(a tree scan gets killed mid-run).
"""

from pathlib import Path

import yaml

_CI = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict:
    return yaml.safe_load(_CI.read_text(encoding="utf-8"))


def test_concurrency_block_present() -> None:
    assert "concurrency" in _workflow(), "ci.yml must declare a top-level concurrency block (move A — supersede-cancel)"


def test_cancel_in_progress_is_conditional_on_pull_request() -> None:
    cancel = str(_workflow()["concurrency"]["cancel-in-progress"])
    assert "pull_request" in cancel, (
        "cancel-in-progress must be gated on pull_request so a superseding push/schedule "
        "run never kills a main-branch tree scan mid-run."
    )
    assert cancel.strip().lower() != "true", (
        "cancel-in-progress must NOT be an unconditional true — that would cancel push/schedule "
        "runs and defeat the main-branch backstops."
    )


def test_group_supersedes_per_pr_but_isolates_non_pr_runs() -> None:
    group = str(_workflow()["concurrency"]["group"])
    assert "pull_request.number" in group, (
        "the concurrency group must key on the PR number so a re-push supersedes its OWN wave "
        "instead of stacking a new one."
    )
    assert "run_id" in group, (
        "push/schedule must fall back to a unique run_id group so their runs are never cancelled "
        "by a superseding sibling."
    )
