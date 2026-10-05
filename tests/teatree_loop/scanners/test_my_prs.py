"""F5.1 — MyPrsScanner fails LOUD when a PR carries no pipeline field.

GitHub's ``search/issues`` list carries no CI status, so a bare search hit drove
the my_pr.failed auto-debug lane with an empty status — the lane was structurally
inert on this deployment's forge. The backend now enriches each hit; the scanner
warns (throttled) about any PR that still arrives WITHOUT a pipeline field rather
than silently classifying it as a benign open PR.
"""

import logging

from teatree.backends.gitlab import GitLabCodeHost
from teatree.loop.dispatch import dispatch
from teatree.loop.scanners.my_prs import MyPrsScanner
from teatree.loop.scanners.my_prs_ci import BoundedCiEnricher, reset_ci_memo
from teatree.utils.throttled_log import reset_throttle
from tests.teatree_backends._gitlab_wire import GitLabWire
from tests.teatree_loop.test_scanners import FakeCodeHost


def _pr_without_pipeline(*, iid: int = 1) -> dict[str, object]:
    return {"iid": iid, "title": "Bare hit", "web_url": f"https://github.com/o/r/pull/{iid}"}


def _pr_with_rollup(*, iid: int = 2, state: str = "success") -> dict[str, object]:
    return {
        "iid": iid,
        "title": "Enriched",
        "web_url": f"https://github.com/o/r/pull/{iid}",
        "status_check_rollup": {"state": state},
    }


def test_warns_once_when_a_pr_has_no_pipeline_field(caplog) -> None:
    reset_throttle()
    host = FakeCodeHost(user="alice", my_prs=[_pr_without_pipeline()])
    with caplog.at_level(logging.WARNING, logger="teatree.loop.scanners.my_prs"):
        signals = MyPrsScanner(host=host).scan()
    # The bare hit still renders as an open PR (no red-lane data to fail on)...
    assert [s.kind for s in signals] == ["my_pr.open"]
    # ...but the gap is surfaced, not silent.
    assert any("no pipeline field" in r.message for r in caplog.records)


def test_no_warning_when_every_pr_is_enriched(caplog) -> None:
    reset_throttle()
    host = FakeCodeHost(user="alice", my_prs=[_pr_with_rollup(state="success")])
    with caplog.at_level(logging.WARNING, logger="teatree.loop.scanners.my_prs"):
        MyPrsScanner(host=host).scan()
    assert not any("no pipeline field" in r.message for r in caplog.records)


def test_enriched_failure_rollup_still_fires_the_failed_lane() -> None:
    reset_throttle()
    host = FakeCodeHost(user="alice", my_prs=[_pr_with_rollup(state="failure")])
    signals = MyPrsScanner(host=host).scan()
    assert [s.kind for s in signals] == ["my_pr.failed"]


class TestCiEnrichmentReachesTheRedLane:
    """A payload with no pipeline field is the GitLab cross-project MR-list shape.

    Left alone it reads as in-progress, so ``my_pr.failed`` cannot fire for it
    however red the MR is. These pin that the enricher closes that hole — and that
    it stays bounded to the PRs this overlay actually claims.
    """

    @staticmethod
    def _mr(*, iid: int = 3) -> dict[str, object]:
        return {
            "iid": iid,
            "title": "Cross-project MR",
            "web_url": f"https://gitlab.example.com/group/repo/-/merge_requests/{iid}",
            "sha": f"{iid:040d}",
        }

    def test_a_red_mr_with_no_pipeline_field_now_fires_the_failed_lane(self) -> None:
        reset_ci_memo()
        reset_throttle()
        host = FakeCodeHost(user="alice", my_prs=[self._mr()])

        signals = MyPrsScanner(host=host, ci_enricher=BoundedCiEnricher(resolve=lambda _url: "failed")).scan()

        assert [s.kind for s in signals] == ["my_pr.failed"]

    def test_an_enriched_green_mr_renders_as_open(self) -> None:
        reset_ci_memo()
        reset_throttle()
        host = FakeCodeHost(user="alice", my_prs=[self._mr()])

        signals = MyPrsScanner(host=host, ci_enricher=BoundedCiEnricher(resolve=lambda _url: "green")).scan()

        assert [s.kind for s in signals] == ["my_pr.open"]

    def test_an_enriched_mr_no_longer_counts_as_structurally_inert(self, caplog) -> None:
        reset_ci_memo()
        reset_throttle()
        host = FakeCodeHost(user="alice", my_prs=[self._mr()])

        with caplog.at_level(logging.WARNING, logger="teatree.loop.scanners.my_prs"):
            MyPrsScanner(host=host, ci_enricher=BoundedCiEnricher(resolve=lambda _url: "green")).scan()

        assert not any("no pipeline field" in r.message for r in caplog.records)

    def test_an_mr_the_enricher_could_not_read_is_still_reported_as_inert(self, caplog) -> None:
        reset_ci_memo()
        reset_throttle()
        host = FakeCodeHost(user="alice", my_prs=[self._mr()])

        with caplog.at_level(logging.WARNING, logger="teatree.loop.scanners.my_prs"):
            signals = MyPrsScanner(host=host, ci_enricher=BoundedCiEnricher(resolve=lambda _url: "")).scan()

        assert [s.kind for s in signals] == ["my_pr.open"]
        assert any("no pipeline field" in r.message for r in caplog.records)

    def test_a_payload_that_already_carries_ci_is_never_re_read(self) -> None:
        reset_ci_memo()
        reset_throttle()
        reads: list[str] = []

        def _resolve(url: str) -> str:
            reads.append(url)
            return "failed"

        host = FakeCodeHost(user="alice", my_prs=[_pr_with_rollup(state="success")])
        MyPrsScanner(host=host, ci_enricher=BoundedCiEnricher(resolve=_resolve)).scan()

        assert reads == []

    def test_a_pr_outside_the_overlays_url_claim_is_never_read(self) -> None:
        reset_ci_memo()
        reset_throttle()
        reads: list[str] = []

        def _resolve(url: str) -> str:
            reads.append(url)
            return "failed"

        host = FakeCodeHost(user="alice", my_prs=[self._mr()])
        MyPrsScanner(
            host=host,
            allowed_url_prefixes=("https://gitlab.example.com/other/",),
            ci_enricher=BoundedCiEnricher(resolve=_resolve),
        ).scan()

        assert reads == []


def _gitlab_mr_whose_pipeline_is(status: str) -> dict[str, object]:
    return {
        "iid": 4,
        "title": "GitLab MR",
        "web_url": "https://gitlab.example.com/group/repo/-/merge_requests/4",
        "head_pipeline": {"status": status},
    }


def _routes(status: str) -> list[tuple[str, str, str]]:
    reset_throttle()
    signals = MyPrsScanner(host=FakeCodeHost(user="alice", my_prs=[_gitlab_mr_whose_pipeline_is(status)])).scan()
    return [(signal.kind, action.kind, action.zone) for signal in signals for action in dispatch([signal])]


class TestOnlyARealFailureDispatchesDebug:
    """Each pipeline state is decided explicitly: skipped is settled, manual waits on a person, failed is broken."""

    def test_a_pipeline_every_job_of_which_was_skipped_is_settled_and_dispatches_no_debug(self) -> None:
        assert _routes("skipped") == [("my_pr.open", "statusline", "in_flight")]

    def test_a_manual_pipeline_waits_for_a_person_and_dispatches_no_debug(self) -> None:
        assert _routes("manual") == [("my_pr.manual_action", "statusline", "action_needed")]

    def test_a_manual_pipeline_is_reported_as_needing_a_manual_action(self) -> None:
        reset_throttle()
        (signal,) = MyPrsScanner(
            host=FakeCodeHost(user="alice", my_prs=[_gitlab_mr_whose_pipeline_is("manual")])
        ).scan()

        assert "waiting for a manual action" in signal.summary

    def test_control_a_failed_pipeline_still_dispatches_debug(self) -> None:
        assert _routes("failed") == [
            ("my_pr.failed", "agent", "t3:debug"),
            ("my_pr.failed", "statusline", "action_needed"),
        ]


def test_a_canceled_merge_train_pipeline_in_front_of_a_green_head_dispatches_no_debug() -> None:
    head, train = "a" * 40, "c" * 40
    mr_url = "https://gitlab.example/group/repo/-/merge_requests/1"
    wire = GitLabWire(
        {
            "user": {"username": "alice"},
            "merge_requests": [{"iid": 1, "project_id": 42, "sha": head, "title": "GitLab MR", "web_url": mr_url}],
            "projects/42/merge_requests/1/pipelines": [
                {"status": "canceled", "sha": train, "source": "merge_train", "ref": "refs/merge-requests/1/train"},
                {"status": "success", "sha": head, "source": "merge_request_event"},
            ],
        }
    )
    reset_throttle()

    signals = MyPrsScanner(host=GitLabCodeHost(client=wire)).scan()

    assert [(signal.kind, action.kind) for signal in signals for action in dispatch([signal])] == [
        ("my_pr.open", "statusline"),
    ]
