"""Behaviour tests for ``IssueImplementerScanner`` — label filter + claim idempotency (#1553).

The scanner is the discovery + claim half of the always-on issue-implementer
loop. It lists the user's open issues via the code-host backend, keeps the
ones carrying the configured label, and claims each through the TOCTOU-safe
:meth:`ImplementedIssueMarker.claim` so a re-tick (or a concurrent overlay)
never double-dispatches the same issue.
"""

from dataclasses import dataclass, field

from django.test import TestCase

from teatree.core.models import ImplementedIssueMarker
from teatree.loop.scanners.issue_implementer import IssueImplementerScanner
from teatree.types import RawAPIDict


@dataclass
class _Host:
    """Minimal CodeHostBackend stub — only the methods the scanner calls."""

    user: str = "alice"
    issues: list[RawAPIDict] = field(default_factory=list)
    open_prs: list[RawAPIDict] = field(default_factory=list)

    def current_user(self) -> str:
        return self.user

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        _ = assignee
        return self.issues

    def list_my_prs(self, *, author: str) -> list[RawAPIDict]:
        _ = author
        return self.open_prs


class IssueImplementerScannerTests(TestCase):
    OVERLAY = "acme"
    LABEL = "auto-implement"
    URL_A = "https://github.com/souliane/teatree/issues/100"
    URL_B = "https://github.com/souliane/teatree/issues/101"

    def _scanner(self, host: _Host, *, label: str = LABEL) -> IssueImplementerScanner:
        return IssueImplementerScanner(host=host, label=label, overlay_name=self.OVERLAY)

    @staticmethod
    def _issue(url: str, *, labels: list[str], title: str = "do it", state: str = "open") -> RawAPIDict:
        return {"web_url": url, "title": title, "labels": labels, "state": state}

    def test_labelled_open_issue_is_claimed_and_emitted(self) -> None:
        host = _Host(issues=[self._issue(self.URL_A, labels=[self.LABEL])])
        signals = self._scanner(host).scan()
        assert [s.kind for s in signals] == ["issue_implementer.claimed"]
        assert signals[0].payload["url"] == self.URL_A
        marker = ImplementedIssueMarker.objects.get(issue_url=self.URL_A, overlay=self.OVERLAY)
        assert marker.state == ImplementedIssueMarker.State.DISPATCHED

    def test_only_labelled_issues_are_actionable(self) -> None:
        host = _Host(
            issues=[
                self._issue(self.URL_A, labels=[self.LABEL]),
                self._issue(self.URL_B, labels=["something-else"]),
            ]
        )
        signals = self._scanner(host).scan()
        assert {s.payload["url"] for s in signals} == {self.URL_A}
        assert not ImplementedIssueMarker.objects.filter(issue_url=self.URL_B).exists()

    def test_closed_labelled_issue_is_skipped(self) -> None:
        host = _Host(issues=[self._issue(self.URL_A, labels=[self.LABEL], state="closed")])
        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.filter(issue_url=self.URL_A).exists()

    def test_second_claim_of_same_issue_is_skipped(self) -> None:
        """``claim`` idempotency: a re-tick on an already-claimed issue emits nothing."""
        host = _Host(issues=[self._issue(self.URL_A, labels=[self.LABEL])])
        first = self._scanner(host).scan()
        assert len(first) == 1
        second = self._scanner(host).scan()
        assert second == []
        assert ImplementedIssueMarker.objects.filter(issue_url=self.URL_A, overlay=self.OVERLAY).count() == 1

    def test_empty_label_claims_nothing(self) -> None:
        """Defence-in-depth: an empty label never picks up any issue."""
        host = _Host(issues=[self._issue(self.URL_A, labels=[self.LABEL])])
        assert self._scanner(host, label="").scan() == []
        assert not ImplementedIssueMarker.objects.exists()

    def test_no_identity_resolves_to_no_scan(self) -> None:
        host = _Host(user="", issues=[self._issue(self.URL_A, labels=[self.LABEL])])
        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.exists()

    def test_explicit_identities_union_dedupes_by_url(self) -> None:
        host = _Host(issues=[self._issue(self.URL_A, labels=[self.LABEL])])
        scanner = IssueImplementerScanner(
            host=host,
            label=self.LABEL,
            overlay_name=self.OVERLAY,
            identities=("alice", "alice-bot"),
        )
        signals = scanner.scan()
        assert len(signals) == 1
        assert ImplementedIssueMarker.objects.filter(issue_url=self.URL_A).count() == 1

    def test_missing_state_field_treated_as_open(self) -> None:
        host = _Host(issues=[{"web_url": self.URL_A, "title": "t", "labels": [self.LABEL]}])
        assert len(self._scanner(host).scan()) == 1

    def test_issue_without_url_is_skipped(self) -> None:
        host = _Host(issues=[{"title": "no url", "labels": [self.LABEL]}])
        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.exists()

    def test_dict_shaped_labels_are_matched(self) -> None:
        host = _Host(issues=[{"web_url": self.URL_A, "title": "t", "labels": [{"name": self.LABEL}]}])
        assert len(self._scanner(host).scan()) == 1


class IssueImplementerReadbackTests(TestCase):
    """Pre-dispatch forge read-back: skip an issue whose work already exists (Stage 1).

    The local claim ledger cannot see another instance's claim, so before
    claiming the scanner reads the forge for an existing ``<num>-*`` branch or a
    referencing PR and skips when found — closing most of the double-claim window.
    """

    OVERLAY = "acme"
    LABEL = "auto-implement"
    URL_A = "https://github.com/souliane/teatree/issues/100"

    @staticmethod
    def _issue(url: str) -> RawAPIDict:
        return {"web_url": url, "title": "do it", "labels": [IssueImplementerReadbackTests.LABEL], "state": "open"}

    def _scanner(self, host: _Host) -> IssueImplementerScanner:
        return IssueImplementerScanner(host=host, label=self.LABEL, overlay_name=self.OVERLAY)

    def test_skips_claim_when_open_pr_branch_exists(self) -> None:
        host = _Host(
            issues=[self._issue(self.URL_A)],
            open_prs=[{"html_url": "https://github.com/souliane/teatree/pull/7", "head": {"ref": "100-feature-x"}}],
        )
        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.filter(issue_url=self.URL_A).exists()

    def test_claims_when_forge_is_clean(self) -> None:
        host = _Host(
            issues=[self._issue(self.URL_A)],
            open_prs=[{"html_url": "https://github.com/souliane/teatree/pull/7", "head": {"ref": "999-unrelated"}}],
        )
        signals = self._scanner(host).scan()
        assert [s.payload["url"] for s in signals] == [self.URL_A]
        assert ImplementedIssueMarker.objects.filter(issue_url=self.URL_A, overlay=self.OVERLAY).exists()

    def test_disabled_readback_claims_without_forge_query(self) -> None:
        host = _Host(
            issues=[self._issue(self.URL_A)],
            open_prs=[{"html_url": "https://github.com/souliane/teatree/pull/7", "head": {"ref": "100-feature-x"}}],
        )
        scanner = IssueImplementerScanner(
            host=host, label=self.LABEL, overlay_name=self.OVERLAY, readback_enabled=False
        )
        signals = scanner.scan()
        assert [s.payload["url"] for s in signals] == [self.URL_A]


class IssueImplementerNeedsTriageGateTests(TestCase):
    """``needs-triage`` blocks auto-implementation even when the implementer label is present.

    The maintainer applies ``needs-triage`` to an issue to withhold it from the
    autonomous factory until they have reviewed it. The scanner is the claim
    chokepoint, so the gate filters such issues out at selection time — they are
    never claimed, never dispatched, and no marker row is written.
    """

    OVERLAY = "acme"
    LABEL = "auto-implement"
    URL_A = "https://github.com/souliane/teatree/issues/200"
    URL_B = "https://github.com/souliane/teatree/issues/201"

    def _scanner(self, host: _Host) -> IssueImplementerScanner:
        return IssueImplementerScanner(host=host, label=self.LABEL, overlay_name=self.OVERLAY)

    @staticmethod
    def _issue(url: str, *, labels: list[str]) -> RawAPIDict:
        return {"web_url": url, "title": "do it", "labels": labels, "state": "open"}

    def test_needs_triage_issue_is_not_claimed(self) -> None:
        host = _Host(issues=[self._issue(self.URL_A, labels=[self.LABEL, "needs-triage"])])
        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.filter(issue_url=self.URL_A).exists()

    def test_needs_triage_does_not_starve_a_clean_sibling(self) -> None:
        host = _Host(
            issues=[
                self._issue(self.URL_A, labels=[self.LABEL, "needs-triage"]),
                self._issue(self.URL_B, labels=[self.LABEL]),
            ]
        )
        signals = self._scanner(host).scan()
        assert {s.payload["url"] for s in signals} == {self.URL_B}
        assert not ImplementedIssueMarker.objects.filter(issue_url=self.URL_A).exists()

    def test_dict_shaped_needs_triage_label_is_honoured(self) -> None:
        host = _Host(
            issues=[
                {
                    "web_url": self.URL_A,
                    "title": "t",
                    "labels": [{"name": self.LABEL}, {"name": "needs-triage"}],
                    "state": "open",
                }
            ]
        )
        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.exists()
