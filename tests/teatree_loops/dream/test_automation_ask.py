"""Dream automatable-ask promotion — the "improve-with-new-stuff" half (#2663).

The structural sibling of the compliance accountant: dreaming detects recurring
MANUAL user asks t3 could automate and promotes each as a fix-and-merge gap under
the standing umbrella, so the user "gets out of the loop". Each ask-cluster is
classified Bucket A (EXISTING_GAP — an existing loop/skill should have handled it)
or Bucket B (NEW_WORKFLOW — no automation exists, e.g. a hotfix lane), then routed
through ``umbrella_ledger.promote_gap``.

These tests drive the classify + promote flow with an INJECTED fake code host and
real ``ConsolidatedMemory`` rows / ``DistilledCluster`` values, so the whole flow
runs without an LLM or a live forge.
"""

from pathlib import Path
from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory
from teatree.loops.dream.automation_ask import (
    AUTOMATION_CATALOG,
    AskBucket,
    AutomationAskFinding,
    classify_ask_cluster,
    cluster_for_row,
    detect_automatable_asks,
    promote_automatable_asks,
    row_looks_like_ask,
    run_automation_asks_phase,
)
from teatree.loops.dream.engine import ConsolidationExtract, DistilledCluster, WeightedSnippet

UMBRELLA = "https://github.com/souliane/teatree/issues/2663"


def _fake_host(*, body: str = "## Open gaps\n") -> CodeHostBackend:
    host = MagicMock(spec=CodeHostBackend)
    host.get_issue.return_value = {"body": body}
    host.update_issue.return_value = {"number": 2663}
    return host


def _ask_snippet(name: str, body: str) -> WeightedSnippet:
    return WeightedSnippet(path=Path(f"/sessions/{name}"), kind="main", weight=100, text=body)


def _extract(*snippets: WeightedSnippet) -> ConsolidationExtract:
    return ConsolidationExtract(snippets=tuple(snippets), truncated=False)


#: An ask whose subject names an existing mechanism — a review/MR reminder the
#: followup loop already owns. Classifying it Bucket A must name that loop.
_EXISTING_GAP_RULE = "Automate the daily follow-up nag for stale open review requests so the user stops asking."
#: An ask with no existing automation — a hotfix lane. Bucket B (NEW_WORKFLOW).
_NEW_WORKFLOW_RULE = "Build a hotfix lane fast-tracking an urgent production rollback the user keeps driving by hand."


def _cluster(
    *,
    key: str = "ask-1",
    rule: str = _NEW_WORKFLOW_RULE,
    source: str = "/sessions/s1.jsonl",
    citation: str = "please set up a hotfix lane",
) -> DistilledCluster:
    return DistilledCluster(
        cluster_key=key,
        rule=rule,
        source_files=[source],
        is_binding=False,
        verified_citation=citation,
        durable_destination="",
    )


class ClassifyAskClusterTestCase(TestCase):
    """Each ask-cluster is Bucket A (existing mechanism) or Bucket B (new workflow)."""

    def test_ask_naming_an_existing_loop_is_bucket_a(self) -> None:
        finding = classify_ask_cluster(_cluster(rule=_EXISTING_GAP_RULE))
        assert finding.bucket is AskBucket.EXISTING_GAP
        assert finding.mechanism  # Bucket A must name a real mechanism from the catalog.

    def test_ask_with_no_existing_automation_is_bucket_b(self) -> None:
        finding = classify_ask_cluster(_cluster(rule=_NEW_WORKFLOW_RULE))
        assert finding.bucket is AskBucket.NEW_WORKFLOW
        assert not finding.mechanism

    def test_catalog_is_injected_so_bucket_a_names_a_real_mechanism(self) -> None:
        # The mechanism named for a Bucket-A finding is one of the catalog entries.
        finding = classify_ask_cluster(_cluster(rule=_EXISTING_GAP_RULE))
        assert finding.mechanism in {entry.name for entry in AUTOMATION_CATALOG}

    def test_injected_classifier_seam_overrides_the_default(self) -> None:
        sentinel = AutomationAskFinding(
            cluster_key="ask-1", bucket=AskBucket.NEW_WORKFLOW, mechanism="", rule=_EXISTING_GAP_RULE
        )
        finding = classify_ask_cluster(_cluster(rule=_EXISTING_GAP_RULE), classifier=lambda _c: sentinel)
        assert finding is sentinel


class DetectAutomatableAsksTestCase(TestCase):
    """Detection grounds each ask-cluster: the cited snippet must be in the extract."""

    def test_a_grounded_ask_cluster_is_detected(self) -> None:
        extract = _extract(_ask_snippet("s1.jsonl", "please set up a hotfix lane for urgent rollbacks"))
        findings = detect_automatable_asks([_cluster()], extract)
        assert len(findings) == 1
        assert findings[0].bucket is AskBucket.NEW_WORKFLOW

    def test_an_ungrounded_ask_cluster_is_dropped(self) -> None:
        # The cited quote does not appear in any extract snippet — not grounded.
        extract = _extract(_ask_snippet("s1.jsonl", "an unrelated line with no such quote"))
        findings = detect_automatable_asks([_cluster()], extract)
        assert findings == []

    def test_a_cluster_citing_a_path_not_in_the_extract_is_dropped(self) -> None:
        extract = _extract(_ask_snippet("other.jsonl", "please set up a hotfix lane"))
        findings = detect_automatable_asks([_cluster(source="/sessions/missing.jsonl")], extract)
        assert findings == []


class PromoteAutomatableAsksTestCase(TestCase):
    """Each grounded ask gap routes through the umbrella ledger as a fix-and-merge."""

    def _grounded_extract(self) -> ConsolidationExtract:
        return _extract(_ask_snippet("s1.jsonl", "please set up a hotfix lane for urgent rollbacks"))

    def test_a_new_workflow_ask_upserts_a_checkbox_and_schedules_a_fix(self) -> None:
        host = _fake_host()
        outcomes = promote_automatable_asks([_cluster()], self._grounded_extract(), host, umbrella_url=UMBRELLA)
        assert len(outcomes) == 1
        assert outcomes[0].filed is True
        host.update_issue.assert_called_once()
        _, kwargs = host.update_issue.call_args
        # The Bucket-B framing rides into the umbrella checkbox title.
        assert "new workflow" in kwargs["body"].lower()

    def test_a_bucket_a_title_names_the_existing_mechanism(self) -> None:
        host = _fake_host()
        extract = _extract(_ask_snippet("s1.jsonl", "automate the daily follow-up nag for stale open review"))
        cluster = _cluster(rule=_EXISTING_GAP_RULE, citation="automate the daily follow-up nag for stale open review")
        promote_automatable_asks([cluster], extract, host, umbrella_url=UMBRELLA)
        _, kwargs = host.update_issue.call_args
        assert "existing gap" in kwargs["body"].lower()

    def test_the_scheduled_fix_ticket_links_the_ask_memory_for_retirement(self) -> None:
        # The ask cluster is persisted as a ConsolidatedMemory row; promotion
        # schedules a gap-fix Ticket linked by cluster_key so reconcile-on-merge
        # retires the ask memory when the fix merges (Part C retire path).
        ConsolidatedMemory.objects.create(
            cluster_key="ask-1",
            rule=_NEW_WORKFLOW_RULE,
            source_files=["/sessions/s1.jsonl"],
            member_count=1,
            max_member_weight=100,
            verified_citation="please set up a hotfix lane",
        )
        host = _fake_host()
        promote_automatable_asks([_cluster()], self._grounded_extract(), host, umbrella_url=UMBRELLA)
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        ticket = Ticket.objects.get(extra__dream_gap_key="ask-1")
        assert ticket.extra["dream_memory_cluster_key"] == "ask-1"

    def test_dry_run_writes_nothing_and_schedules_nothing(self) -> None:
        host = _fake_host()
        outcomes = promote_automatable_asks(
            [_cluster()], self._grounded_extract(), host, umbrella_url=UMBRELLA, dry_run=True
        )
        assert outcomes == []
        host.update_issue.assert_not_called()

    def test_an_ungrounded_ask_is_never_promoted(self) -> None:
        host = _fake_host()
        extract = _extract(_ask_snippet("s1.jsonl", "an unrelated line with no cited quote"))
        outcomes = promote_automatable_asks([_cluster()], extract, host, umbrella_url=UMBRELLA)
        assert outcomes == []
        host.update_issue.assert_not_called()


class RowLooksLikeAskTestCase(TestCase):
    """A persisted ConsolidatedMemory row is an ask cluster when its rule reads as one."""

    def _row(self, *, rule: str, citation: str = "please ship it") -> ConsolidatedMemory:
        return ConsolidatedMemory.objects.create(
            cluster_key="r1",
            rule=rule,
            source_files=["/sessions/s1.jsonl"],
            member_count=1,
            max_member_weight=100,
            verified_citation=citation,
        )

    def test_an_imperative_request_rule_is_an_ask(self) -> None:
        assert row_looks_like_ask(self._row(rule="The user keeps asking us to please open the PR by hand."))

    def test_an_operational_urgency_rule_is_an_ask(self) -> None:
        assert row_looks_like_ask(self._row(rule="Repeatedly the user drives an urgent hotfix rollback manually."))

    def test_a_neutral_lesson_rule_is_not_an_ask(self) -> None:
        assert not row_looks_like_ask(self._row(rule="Computed values are rounded to two decimals."))

    def test_cluster_for_row_reconstructs_a_distilled_cluster(self) -> None:
        row = self._row(rule="please open the PR", citation="please open the PR")
        cluster = cluster_for_row(row)
        assert cluster.cluster_key == row.cluster_key
        assert cluster.verified_citation == row.verified_citation
        assert cluster.source_files == row.source_files


class RunAutomationAsksPhaseTestCase(TestCase):
    """The phase reads persisted ask rows + a rebuilt extract and promotes the grounded."""

    def test_a_grounded_persisted_ask_row_is_promoted(self) -> None:
        ConsolidatedMemory.objects.create(
            cluster_key="ask-1",
            rule="The user keeps asking us to please set up a hotfix lane by hand.",
            source_files=["/sessions/s1.jsonl"],
            member_count=2,
            max_member_weight=100,
            verified_citation="please set up a hotfix lane",
        )
        extract = _extract(_ask_snippet("s1.jsonl", "please set up a hotfix lane for urgent rollbacks"))
        host = _fake_host()
        summary = run_automation_asks_phase(extract, host, umbrella_url=UMBRELLA, dry_run=False)
        assert "1" in summary
        host.update_issue.assert_called_once()

    def test_a_neutral_row_is_not_promoted(self) -> None:
        ConsolidatedMemory.objects.create(
            cluster_key="neutral-1",
            rule="Values are rounded to two decimals consistently.",
            source_files=["/sessions/s1.jsonl"],
            member_count=1,
            max_member_weight=10,
            verified_citation="rounded to two decimals",
        )
        extract = _extract(_ask_snippet("s1.jsonl", "the figure was rounded to two decimals as expected"))
        host = _fake_host()
        summary = run_automation_asks_phase(extract, host, umbrella_url=UMBRELLA, dry_run=False)
        assert summary == ""
        host.update_issue.assert_not_called()

    def test_dry_run_promotes_nothing(self) -> None:
        ConsolidatedMemory.objects.create(
            cluster_key="ask-1",
            rule="please set up a hotfix lane",
            source_files=["/sessions/s1.jsonl"],
            member_count=1,
            max_member_weight=100,
            verified_citation="please set up a hotfix lane",
        )
        extract = _extract(_ask_snippet("s1.jsonl", "please set up a hotfix lane now"))
        host = _fake_host()
        summary = run_automation_asks_phase(extract, host, umbrella_url=UMBRELLA, dry_run=True)
        assert summary == ""
        host.update_issue.assert_not_called()
