"""A verdict's findings reach the PR, or the refusal is loud (#4476, #4968).

The publish is idempotent by a hidden marker, passes the same comment checks as
``review post-comment``, routes through the leak / send-proxy chokepoint, and is
gated by the on-behalf pre-gate. Every path that does NOT post names its own
reason: the failure mode this forecloses is a ``findings_count`` with nothing
behind it and nothing said about why.
"""

import pytest
from django.test import TestCase

from teatree.core.models import ConfigSetting, OnBehalfApproval, ReviewVerdict, SendAudit
from teatree.core.review.verdict_findings import marker_for
from teatree.core.review.verdict_findings_publish import (
    ACTION,
    FindingsPublishError,
    PublishOutcome,
    publish_verdict_findings,
)
from teatree.core.send_proxy import OutboundLeakError
from teatree.types import RawAPIDict
from tests._send_gate import TEST_TERM_REGISTRY_JSON
from tests.teatree_core._on_behalf_gate_helpers import seed_forbidding_posture, seed_permitting_posture

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "e" * 40
_PRIVATE_SLUG = "democorp-engineering/widgets"
_SELF = "owner-login"
_COLLEAGUE = "carol"
_LONG_SUMMARY = "word " * 210


class _FakeHost:
    """The forge surface the publish uses — two methods, both recorded."""

    def __init__(self, comments: list[RawAPIDict] | None = None, *, author: str = _SELF) -> None:
        self.comments: list[RawAPIDict] = list(comments or [])
        self.posted: list[RawAPIDict] = []
        self.author = author

    def get_pr_author(self, *, pr_url: str) -> str:
        _ = pr_url
        return self.author

    def current_user(self) -> str:
        return _SELF

    def list_pr_comments(self, *, repo: str, pr_iid: int) -> list[RawAPIDict]:
        _ = repo, pr_iid
        return list(self.comments)

    def post_pr_comment(self, *, repo: str, pr_iid: int, body: str) -> RawAPIDict:
        self.posted.append({"repo": repo, "pr_iid": pr_iid, "body": body})
        self.comments.append({"body": body})
        return {"html_url": f"https://forge.test/{repo}/pull/{pr_iid}#note-{len(self.posted)}"}


class _UnreadableHost(_FakeHost):
    def list_pr_comments(self, *, repo: str, pr_iid: int) -> list[RawAPIDict]:
        msg = "forge unreachable"
        raise RuntimeError(msg)


class _UnreadableAuthorHost(_FakeHost):
    def get_pr_author(self, *, pr_url: str) -> str:
        msg = "forge unreachable"
        raise RuntimeError(msg)


class _PublishBase(TestCase):
    """Isolate the on-behalf env so the DB store is the sole config tier."""

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_ON_BEHALF_AUTO_ACTIONS", "T3_BANNED_TERMS", "TEATREE_TERM_REGISTRY"):
            monkeypatch.delenv(env, raising=False)
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", TEST_TERM_REGISTRY_JSON)
        ConfigSetting.objects.set_value("send_proxy_allowlist", [f"github:{_PRIVATE_SLUG}"])
        self.monkeypatch = monkeypatch

    @staticmethod
    def _verdict(
        findings: list[object] | None = None, *, slug: str = _PRIVATE_SLUG, verdict: str = "hold"
    ) -> ReviewVerdict:
        return ReviewVerdict.objects.create(
            slug=slug,
            pr_id=4476,
            reviewed_sha=_SHA,
            verdict=verdict,
            reviewer_identity="cold-reviewer",
            findings=findings if findings is not None else [{"severity": "blocker", "summary": "unbounded loop"}],
            blast_class="logic",
            gh_verify_result="green",
        )

    @staticmethod
    def _allow_posting() -> None:
        seed_permitting_posture()
        ConfigSetting.objects.set_value("private_repos", [f"github.com/{_PRIVATE_SLUG}"])


class TestPublishReachesThePr(_PublishBase):
    def test_findings_are_posted_as_one_pr_comment(self) -> None:
        self._allow_posting()
        verdict = self._verdict([{"severity": "blocker", "summary": "unbounded loop", "file": "a.py", "line": 9}])
        host = _FakeHost()

        outcome = publish_verdict_findings(verdict, backend=host)

        assert outcome.published
        assert len(host.posted) == 1
        body = str(host.posted[0]["body"])
        assert "unbounded loop" in body
        assert "a.py:9" in body
        assert marker_for(verdict) in body
        assert outcome.comment_url.startswith("https://forge.test/")

    def test_a_second_publish_does_not_duplicate_the_comment(self) -> None:
        self._allow_posting()
        verdict = self._verdict()
        host = _FakeHost()

        publish_verdict_findings(verdict, backend=host)
        second = publish_verdict_findings(verdict, backend=host)

        assert second.skipped_existing
        assert not second.published
        assert len(host.posted) == 1

    def test_a_verdict_with_no_findings_posts_nothing_and_says_so(self) -> None:
        self._allow_posting()
        host = _FakeHost()

        outcome = publish_verdict_findings(self._verdict([]), backend=host)

        assert not outcome.published
        assert not host.posted
        assert "no findings" in outcome.note


class TestPublishFailsLoud(_PublishBase):
    def test_an_unreadable_comment_list_refuses_rather_than_risking_a_duplicate(self) -> None:
        self._allow_posting()
        with pytest.raises(FindingsPublishError) as exc:
            publish_verdict_findings(self._verdict(), backend=_UnreadableHost())
        assert "refusing to risk a duplicate" in str(exc.value)

    def test_no_resolvable_backend_raises_instead_of_silently_skipping(self) -> None:
        self._allow_posting()
        self.monkeypatch.setattr("teatree.core.backend_factory.code_host_from_overlay", lambda *_a, **_k: None)
        with pytest.raises(FindingsPublishError) as exc:
            publish_verdict_findings(self._verdict())
        assert "no code-host backend resolved" in str(exc.value)

    def test_a_banned_term_bound_for_a_public_repo_is_refused_and_never_posted(self) -> None:
        seed_permitting_posture()
        self.monkeypatch.setenv("TEATREE_TERM_REGISTRY", '{"leak":[],"prose_collider":["democorp"]}')
        verdict = self._verdict([{"severity": "blocker", "summary": "democorp secret in the log"}], slug="pub/repo")
        host = _FakeHost()

        with pytest.raises(OutboundLeakError):
            publish_verdict_findings(verdict, backend=host)
        assert not host.posted


class TestOnBehalfGate(_PublishBase):
    def test_the_shipped_default_withholds_the_post_and_names_both_ways_out(self) -> None:
        ConfigSetting.objects.set_value("private_repos", [f"github.com/{_PRIVATE_SLUG}"])
        verdict = self._verdict()
        host = _FakeHost()

        outcome = publish_verdict_findings(verdict, backend=host)

        assert not outcome.published
        assert not host.posted
        assert ACTION in outcome.blocked_reason

    def test_the_block_is_reported_not_swallowed(self) -> None:
        ConfigSetting.objects.set_value("private_repos", [f"github.com/{_PRIVATE_SLUG}"])
        outcome = publish_verdict_findings(self._verdict(), backend=_FakeHost())
        assert outcome.blocked_reason
        assert not outcome.skipped_existing


class TestCommentChecks(_PublishBase):
    """The published body passes ``review post-comment``'s checks, or is withheld and DMed (#4968)."""

    @pytest.fixture(autouse=True)
    def _dms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.dms: list[str] = []
        monkeypatch.setattr("teatree.core.notify.notify_user", lambda text, **_kw: self.dms.append(text))

    def _assert_withheld(self, outcome: PublishOutcome, host: _FakeHost, check: str) -> None:
        assert not outcome.published
        assert not host.posted
        assert check in outcome.blocked_reason
        assert len(self.dms) == 1

    def test_a_merge_safe_verdict_with_several_anchored_findings_is_withheld_and_dmed(self) -> None:
        self._allow_posting()
        verdict = self._verdict(
            [
                {"severity": "nit", "summary": "rename x", "file": "a.py", "line": 9},
                {"severity": "nit", "summary": "rename y", "file": "b.py", "line": 2},
            ],
            verdict="merge_safe",
        )
        host = _FakeHost()

        outcome = publish_verdict_findings(verdict, backend=host)

        self._assert_withheld(outcome, host, "multi-finding general note")
        assert "rename x" in self.dms[0]
        assert "rename y" in self.dms[0]

    def test_an_unbacked_claim_is_withheld(self) -> None:
        self._allow_posting()
        host = _FakeHost()
        outcome = publish_verdict_findings(
            self._verdict([{"severity": "major", "summary": "the retry helper is missing"}]), backend=host
        )
        self._assert_withheld(outcome, host, "unbacked claim")

    def test_a_stakeholder_handle_is_withheld(self) -> None:
        self._allow_posting()
        host = _FakeHost()
        outcome = publish_verdict_findings(
            self._verdict([{"severity": "nit", "summary": "@bob flagged this loop"}]), backend=host
        )
        self._assert_withheld(outcome, host, "comment bloat")

    def test_an_over_cap_body_is_withheld_on_a_colleague_pr(self) -> None:
        self._allow_posting()
        host = _FakeHost(author=_COLLEAGUE)
        outcome = publish_verdict_findings(self._verdict([{"severity": "nit", "summary": _LONG_SUMMARY}]), backend=host)
        self._assert_withheld(outcome, host, "colleague prose cap")

    def test_the_same_over_cap_body_is_posted_on_the_owners_own_pr(self) -> None:
        self._allow_posting()
        host = _FakeHost()
        outcome = publish_verdict_findings(self._verdict([{"severity": "nit", "summary": _LONG_SUMMARY}]), backend=host)
        assert outcome.published
        assert len(host.posted) == 1

    def test_an_unreadable_author_withholds_an_over_cap_body_and_says_why(self) -> None:
        self._allow_posting()
        verdict = self._verdict([{"severity": "nit", "summary": _LONG_SUMMARY}])
        for host in (_UnreadableAuthorHost(), _FakeHost(author="")):
            outcome = publish_verdict_findings(verdict, backend=host)
            assert not host.posted
            assert "could not be read" in outcome.blocked_reason

    def test_a_clean_body_is_posted_once_with_every_finding_and_the_marker(self) -> None:
        self._allow_posting()
        verdict = self._verdict(
            [
                {"severity": "nit", "summary": "rename x", "file": "a.py", "line": 9},
                {"severity": "minor", "summary": "log the retry", "file": "", "line": 0},
            ],
            verdict="merge_safe",
        )
        host = _FakeHost()

        outcome = publish_verdict_findings(verdict, backend=host)

        assert outcome.published
        assert len(host.posted) == 1
        body = str(host.posted[0]["body"])
        assert "rename x" in body
        assert "log the retry" in body
        assert marker_for(verdict) in body
        assert SendAudit.objects.exists()
        assert not self.dms

    def test_a_withheld_body_spends_no_approval_and_writes_no_send_audit(self) -> None:
        seed_forbidding_posture()
        ConfigSetting.objects.set_value("private_repos", [f"github.com/{_PRIVATE_SLUG}"])
        target = f"{_PRIVATE_SLUG}#4476"
        OnBehalfApproval.record(target, ACTION, "owner")
        host = _FakeHost()

        outcome = publish_verdict_findings(
            self._verdict([{"severity": "major", "summary": "the retry helper is missing"}]), backend=host
        )

        assert "unbacked claim" in outcome.blocked_reason
        assert not host.posted
        assert OnBehalfApproval.objects.filter(target=target, action=ACTION, consumed_at__isnull=True).exists()
        assert not SendAudit.objects.exists()
