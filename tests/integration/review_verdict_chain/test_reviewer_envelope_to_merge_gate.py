"""The reviewing agent's returned envelope is what unblocks the merge sweep (#3654).

Both ends of this chain had green unit tests while the chain itself was dead: the
recorder records a verdict when handed one, and the sweep merges when a verdict
row exists — but nothing told the reviewing agent to RETURN the envelope, so 138
completed reviewing tasks produced zero verdicts and every open PR logged
``solo_overlay_no_review`` forever. Only a test spanning agent-brief → recorder →
``has_independent_cold_review`` → sweep decision catches that class.

The safety floor is asserted, never lowered: the verdict is recorded by the
orchestrator (a different actor than the reviewer), it binds to the live head SHA,
and a reviewing run that hands back no envelope FAILS loudly instead of completing
over a no-op review.
"""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from teatree.agents.attempt_recorder import record_result_envelope
from teatree.agents.prompt import build_system_context
from teatree.agents.result_schema import RESULT_JSON_SCHEMA, check_evidence
from teatree.core.models import AutoReviewDispatch, ReviewVerdict, Task
from teatree.loop.scanners.pr_sweep import PrSummary, PrSweepScanner
from teatree.loop.scanners.pr_sweep_adapters import NullMergeNotifier
from teatree.loop.scanners.pr_sweep_decision import has_independent_cold_review

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = [pytest.mark.django_db, pytest.mark.integration]

_SLUG = "souliane/teatree"
_PR_ID = 3654
_HEAD = "9c1e4f0b7a3d25e86f0c41ba9d73e5028cf16a4b"
_PR_URL = f"https://github.com/{_SLUG}/pull/{_PR_ID}"
_REVIEWER = "cold-reviewer-agent"
_REVIEWER_DEFINITION = Path(__file__).resolve().parents[3] / "agents" / "reviewer.md"


def _reviewing_task() -> Task:
    dispatch = AutoReviewDispatch.enqueue(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD, pr_url=_PR_URL, overlay="teatree")
    assert dispatch is not None
    task = dispatch.task
    assert task is not None
    task.claim(claimed_by="headless-reviewer")
    return task


def _returned_envelope(*, reviewed_sha: str = _HEAD) -> dict[str, object]:
    return {
        "summary": "Independent cold review of the pull request at its live head.",
        "review_verdict": {
            "verdict": "merge_safe",
            "reviewed_sha": reviewed_sha,
            "reviewer_identity": _REVIEWER,
            "gh_verify_result": "green",
            "blast_class": "logic",
            "findings": [],
        },
    }


@dataclass(slots=True)
class _UnusedKeystone:
    """The CLEAR keystone the solo-overlay path never reaches — no PR here carries a CLEAR."""

    def merge_clear(self, *, clear_id: int, human_authorized: str = "") -> tuple[bool, str, str, str, str]:
        msg = "the solo-overlay bypass must never route through the CLEAR keystone"
        raise AssertionError(msg)


@dataclass(slots=True)
class _FakePrApi:
    """The sweep's forge seam — enough to drive the solo-overlay decision ladder."""

    pr: PrSummary
    merge_calls: list[tuple[str, int, str]] = field(default_factory=list)

    def list_open_prs(self, *, slug: str) -> list[PrSummary]:
        return [self.pr] if slug == _SLUG else []

    def merge_pr_squash_bound(self, *, slug: str, pr_id: int, expected_head_oid: str) -> tuple[bool, str]:
        self.merge_calls.append((slug, pr_id, expected_head_oid))
        return True, expected_head_oid

    def main_check_conclusion(self, *, slug: str, check_name: str) -> str:
        return "SUCCESS"


def _green_pr() -> PrSummary:
    return PrSummary(
        slug=_SLUG,
        number=_PR_ID,
        head_sha=_HEAD,
        is_draft=False,
        has_changes_requested=False,
        rollup=(
            {
                "__typename": "CheckRun",
                "name": "test (3.13)",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-07-23T10:00:00Z",
                "completedAt": "2026-07-23T10:05:00Z",
            },
        ),
        url=_PR_URL,
        title=f"PR {_PR_ID}",
        behind_main=False,
        author="souliane",
        same_repo=True,
    )


@pytest.fixture
def sweep(monkeypatch: pytest.MonkeyPatch) -> _FakePrApi:
    """A solo-overlay sweep over one green, own, same-repo PR at ``_HEAD``."""
    monkeypatch.setattr(
        "teatree.core.merge.ci_rollup.CodeHostQuery.required_context_names",
        lambda *args, **kwargs: {"test (3.13)"},
    )
    monkeypatch.setattr(
        "teatree.core.merge.ci_rollup.CodeHostQuery.pr_changed_paths",
        lambda *args, **kwargs: ["src/teatree/agents/attempt_recorder.py"],
    )
    monkeypatch.setattr("teatree.core.review.author_trust.repo_is_internal", lambda *args, **kwargs: True)
    return _FakePrApi(pr=_green_pr())


def _run_sweep(api: _FakePrApi) -> str:
    scanner = PrSweepScanner(
        repos=(_SLUG,),
        api=api,
        keystone=_UnusedKeystone(),
        notifier=NullMergeNotifier(),
        overlay="teatree",
        solo_overlay=True,
        self_identities=("souliane",),
    )
    signals = scanner.scan()
    assert signals, "the sweep emitted no signal for the PR under test"
    return signals[0].kind


class TestReviewerEnvelopeUnblocksTheMergeSweep:
    def test_returned_envelope_records_verdict_and_sweep_stops_flagging_no_review(self, sweep: _FakePrApi) -> None:
        task = _reviewing_task()

        assert _run_sweep(sweep) == "pr_sweep.flag_no_review"
        assert not has_independent_cold_review(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD)

        record_result_envelope(task, _returned_envelope(), phase="reviewing")

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        recorded = ReviewVerdict.objects.get(slug=_SLUG, pr_id=_PR_ID, reviewed_sha=_HEAD)
        assert recorded.is_merge_safe()
        # Maker≠checker: the reviewer handed the verdict back; the orchestrator wrote it.
        assert recorded.reviewer_identity == _REVIEWER

        assert has_independent_cold_review(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD)
        assert _run_sweep(sweep) == "pr_sweep.merged"
        assert sweep.merge_calls == [(_SLUG, _PR_ID, _HEAD)]

    def test_verdict_at_a_stale_head_leaves_the_sweep_flagging_no_review(self, sweep: _FakePrApi) -> None:
        task = _reviewing_task()
        stale = "0" * 39 + "1"

        record_result_envelope(task, _returned_envelope(reviewed_sha=stale), phase="reviewing")

        assert ReviewVerdict.objects.filter(reviewed_sha=stale).exists()
        assert not has_independent_cold_review(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD)
        assert _run_sweep(sweep) == "pr_sweep.flag_no_review"
        assert sweep.merge_calls == []


class TestMissingEnvelopeFailsLoud:
    def test_reviewing_run_with_no_verdict_envelope_fails_the_task(self, sweep: _FakePrApi) -> None:
        task = _reviewing_task()

        attempt = record_result_envelope(
            task, {"summary": "Reviewed the diff.", "decisions": ["looks good to me"]}, phase="reviewing"
        )

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert "review_verdict" in attempt.error
        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert _run_sweep(sweep) == "pr_sweep.flag_no_review"

    def test_envelope_with_an_unpersistable_verdict_fails_the_task(self, sweep: _FakePrApi) -> None:
        task = _reviewing_task()

        record_result_envelope(
            task,
            {"summary": "Reviewed.", "review_verdict": {"verdict": "PASS", "reviewer_identity": _REVIEWER}},
            phase="reviewing",
        )

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()

    def test_blocked_reviewer_still_escalates_instead_of_failing(self) -> None:
        task = _reviewing_task()

        record_result_envelope(
            task,
            {"summary": "No diff access.", "needs_user_input": True, "user_input_reason": "cannot fetch the head"},
            phase="reviewing",
        )

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED


class TestReviewingBriefTeachesTheEnvelopeVocabulary:
    def test_reviewer_definition_asks_for_the_envelope_in_the_schema_vocabulary(self) -> None:
        # The defect itself: the definition told the agent to terminate at a
        # PASS/HOLD verdict, which is neither the envelope nor the schema's words.
        definition = _REVIEWER_DEFINITION.read_text(encoding="utf-8")

        assert "review_verdict" in definition
        assert "merge_safe" in definition
        assert "hold" in definition
        assert "reviewed_sha" in definition
        assert "40" in definition, "the full-length SHA requirement must be spelled out"
        assert "PASS / HOLD" not in definition

    def test_headless_reviewing_brief_names_the_schema_fields_and_values(self) -> None:
        prompt = build_system_context(_reviewing_task(), skills=[])

        assert "review_verdict" in prompt
        assert "merge_safe" in prompt
        assert "hold" in prompt
        assert "reviewed_sha" in prompt
        assert "reviewer_identity" in prompt
        assert "gh_verify_result" in prompt
        assert "blast_class" in prompt
        assert "40-char" in prompt

    def test_reviewing_evidence_gate_accepts_only_the_verdict_channel(self) -> None:
        assert check_evidence(_returned_envelope(), "reviewing") == ""
        assert "review_verdict" in check_evidence({"decisions": ["ok"]}, "reviewing")

    def test_schema_allowed_values_match_the_recorder_vocabulary(self) -> None:
        properties = RESULT_JSON_SCHEMA["properties"]
        verdict_schema = properties["review_verdict"]["properties"]["verdict"]  # type: ignore[index]
        assert verdict_schema["enum"] == ["merge_safe", "hold"]
        assert {choice.value for choice in ReviewVerdict.Verdict} == set(verdict_schema["enum"])
