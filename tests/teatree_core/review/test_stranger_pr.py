"""Stranger-PR policy: ignore-until-admitted, fail-closed (#3634 section 4).

An untrusted author's PR is invisible to the factory until the owner applies the
admit label. Once admitted it is reviewed — but merge authority is untouched, so
it is still never auto-merged.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConfigSetting
from teatree.core.review.stranger_pr import pr_is_admitted
from teatree.loop.scanner_factory_config import stranger_pr_admission
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.reviewer_prs import ReviewerPrsScanner

OWNER = "souliane"
STRANGER = "random-user"
ADMIT = "t3-auto"
URL = "https://github.com/souliane/teatree/pull/42"


def _pr(*, author: str, labels: list[str] | None = None) -> dict[str, object]:
    return {"html_url": URL, "user": {"login": author}, "labels": [{"name": n} for n in labels or []]}


class TestStrangerPrAdmission(TestCase):
    def setUp(self) -> None:
        patcher = patch("teatree.core.review.author_trust.repo_is_internal", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_untrusted_author_without_the_label_is_ignored(self) -> None:
        assert not pr_is_admitted(_pr(author=STRANGER), pr_url=URL, trusted=frozenset({OWNER}), admit_label=ADMIT)

    def test_untrusted_author_with_the_owner_applied_label_is_admitted(self) -> None:
        pr = _pr(author=STRANGER, labels=[ADMIT])

        assert pr_is_admitted(pr, pr_url=URL, trusted=frozenset({OWNER}), admit_label=ADMIT)

    def test_trusted_author_is_admitted_with_no_label(self) -> None:
        assert pr_is_admitted(_pr(author=OWNER), pr_url=URL, trusted=frozenset({OWNER}), admit_label=ADMIT)

    def test_unresolvable_author_is_ignored(self) -> None:
        assert not pr_is_admitted({"html_url": URL}, pr_url=URL, trusted=frozenset({OWNER}), admit_label=ADMIT)

    def test_empty_admit_label_never_admits_a_stranger(self) -> None:
        pr = _pr(author=STRANGER, labels=["", "bug"])

        assert not pr_is_admitted(pr, pr_url=URL, trusted=frozenset({OWNER}), admit_label="")

    def test_unparseable_url_is_ignored(self) -> None:
        assert not pr_is_admitted(
            _pr(author=OWNER), pr_url="https://example.invalid/x", trusted=frozenset({OWNER}), admit_label=ADMIT
        )


class TestReviewerScannerIgnoresStrangerPrs(TestCase):
    """The gate is armed at the scanner, not just available as a predicate."""

    def setUp(self) -> None:
        patcher = patch("teatree.core.review.author_trust.repo_is_internal", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _scan(self, pr: dict[str, object]) -> list[ScanSignal]:
        host = MagicMock(spec=CodeHostBackend)
        host.current_user.return_value = OWNER
        host.list_review_requested_prs.return_value = [pr]
        scanner = ReviewerPrsScanner(
            host=host,
            identities=(OWNER,),
            overlay_name="acme",
            trusted_authors=(OWNER,),
            admit_label=ADMIT,
        )
        return list(scanner.scan())

    def test_unadmitted_stranger_pr_emits_nothing(self) -> None:
        assert self._scan(_pr(author=STRANGER)) == []

    def test_admitted_stranger_pr_is_reviewed(self) -> None:
        signals = self._scan(_pr(author=STRANGER, labels=[ADMIT]))

        assert [s.kind for s in signals] == ["reviewer_pr.unreviewed"]


class TestStrangerPrAdmissionResolution(TestCase):
    """The gate is armed from the same two values intake is built with."""

    def test_defaults_to_the_shipped_admit_label(self) -> None:
        _trusted, admit_label = stranger_pr_admission("")

        assert admit_label == ADMIT

    def test_reads_the_configured_admit_label_and_trusted_set(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_label", "admit-me")
        ConfigSetting.objects.set_value("trusted_issue_authors", [OWNER])

        trusted, admit_label = stranger_pr_admission("")

        assert admit_label == "admit-me"
        assert OWNER in trusted
