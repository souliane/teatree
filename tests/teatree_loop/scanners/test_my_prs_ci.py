"""Bounded per-MR CI enrichment: what the list payload never carried, read once per head SHA.

The enricher exists because a cross-project MR list carries no pipeline field at
all, so every MR in it reads as "no status" — indistinguishable from in-progress,
which is why the red-MR lane could never fire. These cases pin the three
properties that keep the fix from costing a forge call per MR per tick: the
per-tick cap, the per-head-SHA memo, and the refusal to guess.
"""

import pytest

from teatree.core.modelkit.forge_readability import CHECKS_UNREADABLE
from teatree.loop.scanners.my_prs_ci import BoundedCiEnricher, reset_ci_memo

_URL = "https://gitlab.example.com/group/repo/-/merge_requests/7"
_SHA = "a" * 40


@pytest.fixture(autouse=True)
def _clean_memo() -> None:
    reset_ci_memo()


class _RecordingQuery:
    """Stands in for the merge gate's live-forge query, counting the reads it serves."""

    def __init__(self, verdict: str = "green") -> None:
        self.verdict = verdict
        self.calls: list[str] = []

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        return self.verdict


class TestVerdictTranslation:
    @pytest.mark.parametrize(
        ("verdict", "expected"),
        [("green", "success"), ("pending", "pending"), ("failed", "failed")],
    )
    def test_each_forge_verdict_maps_to_the_scanner_vocabulary(self, verdict: str, expected: str) -> None:
        enricher = BoundedCiEnricher(resolve=_RecordingQuery(verdict))

        assert enricher.status_for(url=_URL, head_sha=_SHA) == expected

    def test_an_unrecognised_verdict_is_not_guessed(self) -> None:
        """An unknown answer stays empty — never coerced into a green or a red."""
        enricher = BoundedCiEnricher(resolve=_RecordingQuery("something-new"))

        assert enricher.status_for(url=_URL, head_sha=_SHA) == ""

    def test_an_unreadable_forge_is_not_a_red_merge_request(self) -> None:
        """An unreadable pipeline must not route the MR to the debug agent.

        The merge gate refuses on ``unreadable`` exactly as it refuses on ``failed``
        — but a forge nobody could read is not a broken MR, and dispatching a
        debugger at one spends an agent on a pipeline that was never seen. The
        empty status is the same "nobody looked yet" an unread MR already gets.
        """
        enricher = BoundedCiEnricher(resolve=_RecordingQuery(CHECKS_UNREADABLE))

        assert enricher.status_for(url=_URL, head_sha=_SHA) == ""

    def test_a_genuinely_failing_pipeline_is_still_dispatched(self) -> None:
        # The over-correction guard beside it: a real red still reaches the lane.
        assert BoundedCiEnricher(resolve=_RecordingQuery("failed")).status_for(url=_URL, head_sha=_SHA) == "failed"

    def test_a_read_failure_leaves_the_status_unknown(self) -> None:
        def _boom(_url: str) -> str:
            unreachable = "forge down"
            raise RuntimeError(unreachable)

        enricher = BoundedCiEnricher(resolve=_boom)

        assert enricher.status_for(url=_URL, head_sha=_SHA) == ""


class TestBoundedness:
    def test_the_per_tick_cap_stops_further_reads(self) -> None:
        query = _RecordingQuery()
        enricher = BoundedCiEnricher(resolve=query, max_per_tick=2)

        resolved = [enricher.status_for(url=f"{_URL}{n}", head_sha=f"{n:040d}") for n in range(5)]

        assert len(query.calls) == 2
        assert resolved == ["success", "success", "", "", ""]

    def test_a_fresh_tick_gets_a_fresh_budget(self) -> None:
        """The budget lives on the instance, which the loop rebuilds each tick."""
        query = _RecordingQuery()
        BoundedCiEnricher(resolve=query, max_per_tick=1).status_for(url=_URL, head_sha=_SHA)
        BoundedCiEnricher(resolve=query, max_per_tick=1).status_for(url=f"{_URL}0", head_sha="b" * 40)

        assert len(query.calls) == 2


class TestMemoIsKeyedOnTheHeadSha:
    def test_a_second_look_at_the_same_head_costs_no_forge_call(self) -> None:
        query = _RecordingQuery()
        first = BoundedCiEnricher(resolve=query).status_for(url=_URL, head_sha=_SHA)
        second = BoundedCiEnricher(resolve=query).status_for(url=_URL, head_sha=_SHA)

        assert (first, second) == ("success", "success")
        assert len(query.calls) == 1

    def test_a_memo_hit_does_not_spend_the_tick_budget(self) -> None:
        query = _RecordingQuery()
        BoundedCiEnricher(resolve=query).status_for(url=_URL, head_sha=_SHA)

        enricher = BoundedCiEnricher(resolve=query, max_per_tick=1)
        enricher.status_for(url=_URL, head_sha=_SHA)
        fresh = enricher.status_for(url=f"{_URL}0", head_sha="b" * 40)

        assert fresh == "success"

    def test_a_new_push_is_read_again(self) -> None:
        query = _RecordingQuery()
        enricher = BoundedCiEnricher(resolve=query)
        enricher.status_for(url=_URL, head_sha=_SHA)
        enricher.status_for(url=_URL, head_sha="b" * 40)

        assert len(query.calls) == 2

    def test_an_unresolved_read_is_not_memoised(self) -> None:
        """Caching a failure would freeze the MR as unknown for the life of the process."""
        failing = _RecordingQuery("")
        BoundedCiEnricher(resolve=failing).status_for(url=_URL, head_sha=_SHA)

        working = _RecordingQuery("failed")
        retried = BoundedCiEnricher(resolve=working).status_for(url=_URL, head_sha=_SHA)

        assert retried == "failed"


class TestRefusesToActWithoutAnAnchor:
    @pytest.mark.parametrize(("url", "sha"), [("", _SHA), (_URL, "")])
    def test_a_missing_url_or_head_sha_is_never_read(self, url: str, sha: str) -> None:
        query = _RecordingQuery()

        assert BoundedCiEnricher(resolve=query).status_for(url=url, head_sha=sha) == ""
        assert query.calls == []
