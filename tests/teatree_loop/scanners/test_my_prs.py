"""F5.1 — MyPrsScanner fails LOUD when a PR carries no pipeline field.

GitHub's ``search/issues`` list carries no CI status, so a bare search hit drove
the my_pr.failed auto-debug lane with an empty status — the lane was structurally
inert on this deployment's forge. The backend now enriches each hit; the scanner
warns (throttled) about any PR that still arrives WITHOUT a pipeline field rather
than silently classifying it as a benign open PR.
"""

import logging

from teatree.loop.scanners.my_prs import MyPrsScanner, _has_pipeline_field
from teatree.utils.throttled_log import reset_throttle
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


def test_has_pipeline_field_detects_each_forge_shape() -> None:
    assert _has_pipeline_field({"head_pipeline": {"status": "failed"}})
    assert _has_pipeline_field({"status_check_rollup": {"state": "failure"}})
    assert _has_pipeline_field({"mergeable_state": "blocked"})
    assert not _has_pipeline_field({"iid": 1, "title": "no ci"})


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
