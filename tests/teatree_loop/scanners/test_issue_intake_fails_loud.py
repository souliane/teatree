"""Intake never reports a queue it could not READ as a queue that is EMPTY.

The outage these pin: a wedged gpg keybox made every ``pass`` read hang, so the GitLab
token was unreadable and both discovery queries raised. ``_collect`` swallowed each one,
``_candidate_issues`` returned ``[]``, and the tick reported ``pending: 0`` — identical to
an idle factory. Eight issues sat unadmitted for a day with every surface reading green.

``ScannerError`` is the seam that already exists for exactly this (its own docstring names
the conflation); the intake scanner simply never raised it.
"""

from dataclasses import dataclass, field

import pytest
from django.test import TestCase

from teatree.core.backend_protocols import CodeHostBackend
from teatree.loop.scanners.issue_intake import IssueIntakeScanner
from teatree.types import RawAPIDict, ScannerError, ScannerErrorClass

ADMIT_LABEL = "t3-auto"
OWNER = "souliane"


@dataclass
class _BrokenHost:
    """A forge whose every read raises — the shape a missing credential produces."""

    error: Exception
    #: Queries that were attempted, so a test can assert the scanner did not stop early.
    attempts: list[str] = field(default_factory=list)

    def current_user(self) -> str:
        return OWNER

    def list_authored_issues(self, *, author: str, repo_slugs: tuple[str, ...] = ()) -> list[RawAPIDict]:
        self.attempts.append(f"authored:{author}")
        raise self.error

    def list_labeled_issues(self, *, label: str, repo_slugs: tuple[str, ...] = ()) -> list[RawAPIDict]:
        self.attempts.append(f"labeled:{label}")
        raise self.error


@dataclass
class _PartlyBrokenHost:
    """One query answers, the other raises — the #3508 fault-isolation case."""

    issues: list[RawAPIDict]

    def current_user(self) -> str:
        return OWNER

    def list_authored_issues(self, *, author: str, repo_slugs: tuple[str, ...] = ()) -> list[RawAPIDict]:
        msg = f"simulated rate limit for {author}"
        raise RuntimeError(msg)

    def list_labeled_issues(self, *, label: str, repo_slugs: tuple[str, ...] = ()) -> list[RawAPIDict]:
        return list(self.issues)


def _scanner(host: CodeHostBackend) -> IssueIntakeScanner:
    return IssueIntakeScanner(
        host=host,
        admit_label=ADMIT_LABEL,
        overlay_name="t3-teatree",
        trusted_authors=(OWNER,),
        identities=(OWNER,),
        readback_enabled=False,
    )


class TestEveryDiscoveryQueryFailing(TestCase):
    """A total discovery failure is UNKNOWN, and must never be reported as empty."""

    def test_raises_scanner_error_instead_of_returning_no_signals(self) -> None:
        host = _BrokenHost(error=RuntimeError("reading secret 'gitlab/pat' timed out after 20s"))
        with pytest.raises(ScannerError) as caught:
            _scanner(host).scan()

        assert caught.value.error_class == ScannerErrorClass.AUTH

    def test_the_error_names_the_scanner_and_the_underlying_failure(self) -> None:
        host = _BrokenHost(error=RuntimeError("reading secret 'gitlab/pat' timed out after 20s"))
        with pytest.raises(ScannerError) as caught:
            _scanner(host).scan()

        message = str(caught.value)
        assert "issue_intake" in message
        assert "gitlab/pat" in message

    def test_every_query_is_still_attempted_before_the_failure_is_raised(self) -> None:
        """The verdict is "all of them failed", so all of them must have been tried."""
        host = _BrokenHost(error=RuntimeError("boom"))
        with pytest.raises(ScannerError):
            _scanner(host).scan()

        assert host.attempts == [f"authored:{OWNER}", f"labeled:{ADMIT_LABEL}"]


class TestTheFailureDetailIsRedacted(TestCase):
    """The detail is DM'd to the owner, so a credential a forge echoed must not ride along."""

    #: Deliberately not credential-shaped — the leak gate blocks a realistic value even in a
    #: fixture, and the redaction keys on the assignment's NAME, never on the value's shape.
    VALUE = "PLACEHOLDER-NOT-A-CREDENTIAL"

    def test_a_credential_in_the_forge_exception_never_reaches_the_detail(self) -> None:
        host = _BrokenHost(error=RuntimeError(f"401 on GET /issues?token={self.VALUE}"))
        with pytest.raises(ScannerError) as caught:
            _scanner(host).scan()

        detail = caught.value.detail or ""
        assert self.VALUE not in detail, f"the DM'd detail carries the credential verbatim: {detail}"
        assert "token=<redacted>" in detail, detail

    def test_an_authorization_header_in_the_forge_exception_is_redacted(self) -> None:
        host = _BrokenHost(error=RuntimeError(f"rejected — Authorization: Bearer {self.VALUE}"))
        with pytest.raises(ScannerError) as caught:
            _scanner(host).scan()

        detail = caught.value.detail or ""
        assert self.VALUE not in detail, f"the DM'd detail carries the credential verbatim: {detail}"
        assert "Authorization: <redacted>" in detail, detail

    def test_an_env_assignment_in_the_forge_exception_is_redacted(self) -> None:
        host = _BrokenHost(error=RuntimeError(f"command failed: GITLAB_TOKEN={self.VALUE} glab api /issues"))
        with pytest.raises(ScannerError) as caught:
            _scanner(host).scan()

        detail = caught.value.detail or ""
        assert self.VALUE not in detail, f"the DM'd detail carries the credential verbatim: {detail}"
        assert "GITLAB_TOKEN=<redacted>" in detail, detail

    def test_the_query_label_still_names_which_read_failed(self) -> None:
        """Redaction strips values, never the diagnosis — an unnamed failure is unactionable."""
        host = _BrokenHost(error=RuntimeError("401 Unauthorized"))
        with pytest.raises(ScannerError) as caught:
            _scanner(host).scan()

        detail = caught.value.detail or ""
        assert f"list_authored_issues({OWNER})" in detail, detail
        assert f"list_labeled_issues({ADMIT_LABEL})" in detail, detail
        assert "401 Unauthorized" in detail, detail


class TestPartialDiscoveryFailure(TestCase):
    """One failing query must NOT take the tick down — the #3508 isolation still holds."""

    def test_a_surviving_query_still_yields_its_candidates(self) -> None:
        issue: RawAPIDict = {
            "web_url": "https://gitlab.com/acme/widget/-/issues/7",
            "state": "opened",
            "title": "a real candidate",
            "labels": [ADMIT_LABEL],
            "author": {"username": OWNER},
            "created_at": "2026-08-25T04:52:00Z",
        }
        signals = _scanner(_PartlyBrokenHost(issues=[issue])).scan()

        assert [signal.payload["url"] for signal in signals] == [issue["web_url"]]


class TestGenuinelyEmptyQueue(TestCase):
    """An empty queue that was actually READ stays an ordinary empty result."""

    def test_no_candidates_returns_no_signals_without_raising(self) -> None:
        @dataclass
        class _EmptyHost:
            def current_user(self) -> str:
                return OWNER

            def list_authored_issues(self, *, author: str, repo_slugs: tuple[str, ...] = ()) -> list[RawAPIDict]:
                return []

            def list_labeled_issues(self, *, label: str, repo_slugs: tuple[str, ...] = ()) -> list[RawAPIDict]:
                return []

        assert _scanner(_EmptyHost()).scan() == []
