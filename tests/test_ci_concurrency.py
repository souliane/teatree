"""Guard: the CI concurrency block supersede-cancels PR waves, never main.

Move (A) of the CI-runtime fix: a re-push to a PR must cancel its OWN superseded
in-progress wave (killing the self-inflicted queue stacking), while push-to-main
and the schedule must NEVER be cancelled — their full-tree banned-terms /
overlay-leak backstops must run to completion. Each group is evaluated per event,
so swapping an expression's branches fails here even though every substring survives.
"""

from typing import Any

import pytest

from tests._actions_workflow import CI_DAILY_CRON, CI_WEEKLY_CRON, Value, github_context, load, render

_PULL_REQUEST = github_context("pull_request", ref="refs/pull/42/merge")


def _concurrency(workflow: str, github: dict[str, Any]) -> tuple[Value, Value]:
    block = load(workflow)["concurrency"]
    context = {"github": github}
    return render(block["group"], context), render(block["cancel-in-progress"], context)


class TestCiSupersedesOnlyPullRequestWaves:
    def test_a_pull_request_re_push_cancels_its_own_wave(self) -> None:
        assert _concurrency("ci.yml", _PULL_REQUEST) == ("ci-pr-42", True)

    @pytest.mark.parametrize(
        "github",
        [
            github_context("push"),
            github_context("schedule", schedule=CI_DAILY_CRON),
            github_context("schedule", schedule=CI_WEEKLY_CRON),
        ],
        ids=["push", "daily", "weekly"],
    )
    def test_main_and_scheduled_runs_get_a_unique_never_cancelled_group(self, github: dict[str, Any]) -> None:
        assert _concurrency("ci.yml", github) == (f"ci-{github['run_id']}", False)


class TestPublishImageNeverCancelsAMainPublish:
    def test_a_pull_request_build_supersedes_only_its_own_earlier_build(self) -> None:
        assert _concurrency("publish-image.yml", _PULL_REQUEST) == ("publish-image-refs/pull/42/merge", True)

    @pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
    def test_main_and_dispatch_publishes_share_one_never_cancelled_group(self, event: str) -> None:
        assert _concurrency("publish-image.yml", github_context(event)) == ("publish-image", False)
