"""A verdict's findings reach a colleague's PR, never the factory's own, or the refusal is loud (#4476, #4968).

The publish is idempotent by a hidden marker, passes the same comment checks as
``review post-comment``, routes through the leak / send-proxy chokepoint, and is
gated by the on-behalf pre-gate. Every path that does NOT post names its own
reason: the failure mode this forecloses is a ``findings_count`` with nothing
behind it and nothing said about why.
"""

from collections.abc import Mapping
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.backends.github.client import GitHubCodeHost
from teatree.core.backend_protocols import HEAD_SHA_UNREADABLE, PartialReviewPublishError, PrReview, PrReviewComment
from teatree.core.models import ConfigSetting, OnBehalfApproval, OnBehalfAudit, ReviewVerdict, SendAudit
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
_BOT = "factory-bot"
_COLLEAGUE = "carol"
_LONG_SUMMARY = "word " * 210
_TODO_DIFF = "@@ -9,0 +10,4 @@\n+def retry():\n+    pass\n+    # TODO: bound the retry loop\n+    return\n"


class _FakeHost:
    """The forge surface the publish uses — every submitted review is recorded."""

    def __init__(self, *, author: str = _COLLEAGUE, live_head: str = _SHA) -> None:
        self.reviews: list[PrReview] = []
        self.author = author
        self.live_head = live_head
        self.diff_reads = 0

    def get_pr_author(self, *, pr_url: str) -> str:
        _ = pr_url
        return self.author

    def current_user(self) -> str:
        return _SELF

    def fetch_live_head_sha(self, *, slug: str, pr_id: int) -> str:
        _ = slug, pr_id
        return self.live_head

    def find_pr_review(self, *, repo: str, pr_iid: int, marker: str) -> bool:
        _ = repo, pr_iid
        return any(review.marker == marker for review in self.reviews)

    def get_pr_file_diffs(self, *, repo: str, pr_iid: int) -> Mapping[str, str | None]:
        _ = repo, pr_iid
        self.diff_reads += 1
        return {"a.py": _TODO_DIFF}

    def submit_pr_review(self, *, repo: str, pr_iid: int, review: PrReview) -> RawAPIDict:
        self.reviews.append(review)
        return {"html_url": f"https://forge.test/{repo}/pull/{pr_iid}#review-{len(self.reviews)}"}


class _RefusingHost(_FakeHost):
    def submit_pr_review(self, *, repo: str, pr_iid: int, review: PrReview) -> RawAPIDict:
        _ = repo, pr_iid, review
        return {"error": "Unprocessable Entity: line must be part of the diff"}


class _RaisingHost(_FakeHost):
    def submit_pr_review(self, *, repo: str, pr_iid: int, review: PrReview) -> RawAPIDict:
        _ = repo, pr_iid, review
        msg = "gh api exited 1"
        raise RuntimeError(msg)


class _PartialHost(_FakeHost):
    def submit_pr_review(self, *, repo: str, pr_iid: int, review: PrReview) -> RawAPIDict:
        _ = repo, pr_iid, review
        raise PartialReviewPublishError(landed=1, total=3)


class _VanishingHost(_FakeHost):
    """Accepts the submit but never shows it on a re-read."""

    def submit_pr_review(self, *, repo: str, pr_iid: int, review: PrReview) -> RawAPIDict:
        _ = review
        return {"html_url": f"https://forge.test/{repo}/pull/{pr_iid}#review-1"}


class _UnreadableHost(_FakeHost):
    def find_pr_review(self, *, repo: str, pr_iid: int, marker: str) -> bool:
        msg = "forge unreachable"
        raise RuntimeError(msg)


class _UnreadableDiffHost(_FakeHost):
    def get_pr_file_diffs(self, *, repo: str, pr_iid: int) -> dict[str, str]:
        msg = "forge unreachable"
        raise RuntimeError(msg)


class _UnreadableAuthorHost(_FakeHost):
    def get_pr_author(self, *, pr_url: str) -> str:
        msg = "forge unreachable"
        raise RuntimeError(msg)


class _UnreadableLoginHost(_FakeHost):
    def current_user(self) -> str:
        msg = "forge unreachable"
        raise RuntimeError(msg)


class _BotTokenHost(_FakeHost):
    def current_user(self) -> str:
        return _BOT


class _OmittedPatchHost(_FakeHost):
    """GitHub answers a file too large or binary to diff with no ``patch``; the real client reads that answer."""

    def get_pr_file_diffs(self, *, repo: str, pr_iid: int) -> Mapping[str, str | None]:
        with patch("teatree.backends.github.client._gh_api_get_paginated", return_value=[{"filename": "a.py"}]):
            return GitHubCodeHost(token="t").get_pr_file_diffs(repo=repo, pr_iid=pr_iid)


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
        findings: list[object] | None = None, *, slug: str = _PRIVATE_SLUG, verdict: str = "hold", sha: str = _SHA
    ) -> ReviewVerdict:
        return ReviewVerdict.objects.create(
            slug=slug,
            pr_id=4476,
            reviewed_sha=sha,
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
    def test_findings_are_submitted_as_one_inline_review(self) -> None:
        self._allow_posting()
        verdict = self._verdict(
            [
                {"severity": "blocker", "summary": "unbounded loop", "file": "a.py", "line": 9},
                {"severity": "minor", "summary": "log the retry", "file": "", "line": 0},
            ]
        )
        host = _FakeHost()

        outcome = publish_verdict_findings(verdict, host_kind="github", backend=host)

        assert outcome.published
        assert len(host.reviews) == 1
        review = host.reviews[0]
        assert review.comments == (PrReviewComment(path="a.py", line=9, body="unbounded loop"),)
        assert "log the retry" in review.body
        assert review.marker == marker_for(verdict)
        assert review.commit_sha == _SHA
        assert outcome.comment_url.startswith("https://forge.test/")

    def test_a_second_publish_does_not_duplicate_the_review(self) -> None:
        self._allow_posting()
        verdict = self._verdict()
        host = _FakeHost()

        publish_verdict_findings(verdict, host_kind="github", backend=host)
        second = publish_verdict_findings(verdict, host_kind="github", backend=host)

        assert second.skipped_existing
        assert not second.published
        assert len(host.reviews) == 1

    def test_a_verdict_with_no_findings_posts_nothing_and_says_so(self) -> None:
        self._allow_posting()
        host = _FakeHost()

        outcome = publish_verdict_findings(self._verdict([]), host_kind="github", backend=host)

        assert not outcome.published
        assert not host.reviews
        assert "no findings" in outcome.note

    def test_an_unknown_forge_withholds_before_any_forge_read(self) -> None:
        self._allow_posting()
        host = _UnreadableAuthorHost()

        outcome = publish_verdict_findings(self._verdict(), host_kind="", backend=host)

        assert not host.reviews
        assert "the forge hosting democorp-engineering/widgets is unknown" in outcome.blocked_reason

    def test_a_moved_head_withholds_the_review(self) -> None:
        self._allow_posting()
        host = _FakeHost(live_head="f" * 40)

        outcome = publish_verdict_findings(self._verdict(), host_kind="github", backend=host)

        assert not host.reviews
        assert "head moved" in outcome.blocked_reason

    def test_an_unreadable_head_withholds_the_review_naming_it_unreadable(self) -> None:
        self._allow_posting()
        host = _FakeHost(live_head=HEAD_SHA_UNREADABLE)

        outcome = publish_verdict_findings(self._verdict(), host_kind="github", backend=host)

        assert not host.reviews
        assert "head unreadable" in outcome.blocked_reason
        assert "\x00" not in outcome.blocked_reason


class TestSelfAuthoredPrGetsNothing(_PublishBase):
    @pytest.fixture(autouse=True)
    def _dms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.dms: list[str] = []
        monkeypatch.setattr("teatree.core.notify.notify_user", lambda text, **_kw: self.dms.append(text))

    def test_an_own_pr_gets_no_review_and_no_dm(self) -> None:
        self._allow_posting()
        host = _FakeHost(author=_SELF)

        for sha, verdict_word in (("1" * 40, "hold"), ("2" * 40, "merge_safe")):
            outcome = publish_verdict_findings(
                self._verdict(verdict=verdict_word, sha=sha), host_kind="github", backend=host
            )

            assert outcome.self_review
            assert not outcome.published
            assert not outcome.blocked_reason
            assert "self-authored" in outcome.note
        assert not host.reviews
        assert not self.dms
        assert not SendAudit.objects.exists()

    def test_an_own_pr_is_not_published_even_when_the_on_behalf_gate_would_block(self) -> None:
        ConfigSetting.objects.set_value("private_repos", [f"github.com/{_PRIVATE_SLUG}"])
        outcome = publish_verdict_findings(self._verdict(), host_kind="github", backend=_FakeHost(author=_SELF))
        assert outcome.self_review
        assert not self.dms

    def test_an_unreadable_author_withholds_without_a_dm(self) -> None:
        self._allow_posting()
        verdict = self._verdict()
        for host in (_UnreadableAuthorHost(), _FakeHost(author="")):
            outcome = publish_verdict_findings(verdict, host_kind="github", backend=host)
            assert not host.reviews
            assert "could not be read" in outcome.blocked_reason
        assert not self.dms

    def test_an_own_pr_is_withheld_when_our_own_login_cannot_be_read(self) -> None:
        self._allow_posting()
        self.monkeypatch.setattr("teatree.core.self_forge_identities._configured_aliases", tuple)
        host = _UnreadableLoginHost(author=_SELF)

        outcome = publish_verdict_findings(self._verdict(), host_kind="github", backend=host)

        assert not outcome.published
        assert "could not be confirmed" in outcome.blocked_reason
        assert not host.reviews
        assert not self.dms

    def test_a_bot_token_with_no_owner_alias_cannot_tell_the_owner_from_a_colleague(self) -> None:
        self._allow_posting()
        self.monkeypatch.setattr("teatree.core.self_forge_identities._configured_aliases", tuple)
        self.monkeypatch.setattr("teatree.core.self_forge_identities.declared_identities_for_url", lambda _url: (_BOT,))
        own, bots = _BotTokenHost(author=_SELF), _BotTokenHost(author=_BOT)

        withheld = publish_verdict_findings(self._verdict(), host_kind="github", backend=own)
        skipped = publish_verdict_findings(self._verdict(sha="3" * 40), host_kind="github", backend=bots)

        assert "could not be confirmed" in withheld.blocked_reason
        assert skipped.self_review
        assert not own.reviews
        assert not bots.reviews
        assert not self.dms


class TestPublishFailsLoud(_PublishBase):
    def test_an_unreadable_review_list_refuses_rather_than_risking_a_duplicate(self) -> None:
        self._allow_posting()
        with pytest.raises(FindingsPublishError) as exc:
            publish_verdict_findings(self._verdict(), host_kind="github", backend=_UnreadableHost())
        assert "refusing to risk a duplicate" in str(exc.value)

    def test_no_resolvable_backend_raises_instead_of_silently_skipping(self) -> None:
        self._allow_posting()
        self.monkeypatch.setattr("teatree.core.backend_factory.code_host_from_overlay", lambda *_a, **_k: None)
        with pytest.raises(FindingsPublishError) as exc:
            publish_verdict_findings(self._verdict(), host_kind="github")
        assert "no code-host backend resolved" in str(exc.value)

    def test_a_banned_term_bound_for_a_public_repo_is_refused_and_never_posted(self) -> None:
        seed_permitting_posture()
        self.monkeypatch.setenv("TEATREE_TERM_REGISTRY", '{"leak":[],"prose_collider":["democorp"]}')
        verdict = self._verdict([{"severity": "blocker", "summary": "democorp secret in the log"}], slug="pub/repo")
        host = _FakeHost()

        with pytest.raises(OutboundLeakError):
            publish_verdict_findings(verdict, host_kind="github", backend=host)
        assert not host.reviews

    def test_a_forge_refusal_is_not_reported_as_published(self) -> None:
        self._allow_posting()
        verdict = self._verdict()
        for host in (_RefusingHost(), _RaisingHost()):
            with pytest.raises(FindingsPublishError) as exc:
                publish_verdict_findings(verdict, host_kind="github", backend=host)
            assert "refused the findings review" in str(exc.value)

    def test_a_forge_refusal_spends_no_approval(self) -> None:
        seed_forbidding_posture()
        ConfigSetting.objects.set_value("private_repos", [f"github.com/{_PRIVATE_SLUG}"])
        target = f"{_PRIVATE_SLUG}#4476"
        OnBehalfApproval.record(target, ACTION, "owner")

        with pytest.raises(FindingsPublishError):
            publish_verdict_findings(self._verdict(), host_kind="github", backend=_RefusingHost())

        assert OnBehalfApproval.objects.filter(target=target, action=ACTION, consumed_at__isnull=True).exists()
        assert not OnBehalfAudit.objects.exists()

    def test_a_partial_post_is_loud_and_audits_what_landed(self) -> None:
        seed_forbidding_posture()
        ConfigSetting.objects.set_value("private_repos", [f"github.com/{_PRIVATE_SLUG}"])
        target = f"{_PRIVATE_SLUG}#4476"
        OnBehalfApproval.record(target, ACTION, "owner")

        with pytest.raises(FindingsPublishError) as exc:
            publish_verdict_findings(self._verdict(), host_kind="github", backend=_PartialHost())

        assert "1 of 3" in str(exc.value)
        assert OnBehalfAudit.objects.filter(target=target, action=ACTION).exists()

    def test_a_review_that_does_not_read_back_is_not_reported_as_published(self) -> None:
        self._allow_posting()
        with pytest.raises(FindingsPublishError) as exc:
            publish_verdict_findings(self._verdict(), host_kind="github", backend=_VanishingHost())
        assert "does not read back" in str(exc.value)


class TestOnBehalfGate(_PublishBase):
    def test_the_shipped_default_withholds_the_post_and_names_both_ways_out(self) -> None:
        ConfigSetting.objects.set_value("private_repos", [f"github.com/{_PRIVATE_SLUG}"])
        verdict = self._verdict()
        host = _FakeHost()

        outcome = publish_verdict_findings(verdict, host_kind="github", backend=host)

        assert not outcome.published
        assert not host.reviews
        assert ACTION in outcome.blocked_reason

    def test_the_block_is_reported_not_swallowed(self) -> None:
        ConfigSetting.objects.set_value("private_repos", [f"github.com/{_PRIVATE_SLUG}"])
        outcome = publish_verdict_findings(self._verdict(), host_kind="github", backend=_FakeHost())
        assert outcome.blocked_reason
        assert not outcome.skipped_existing

    def test_a_block_on_a_colleague_pr_dms_the_findings_once(self) -> None:
        dms: list[str] = []
        self.monkeypatch.setattr("teatree.core.notify.notify_user", lambda text, **_kw: dms.append(text))
        ConfigSetting.objects.set_value("private_repos", [f"github.com/{_PRIVATE_SLUG}"])

        publish_verdict_findings(self._verdict(), host_kind="github", backend=_FakeHost())

        assert len(dms) == 1
        assert "unbounded loop" in dms[0]

    def test_a_blocked_review_writes_no_send_audit_for_any_of_its_bodies_on_any_retry(self) -> None:
        self.monkeypatch.setattr("teatree.core.notify.notify_user", lambda *_a, **_kw: None)
        seed_forbidding_posture()
        ConfigSetting.objects.set_value("private_repos", [f"github.com/{_PRIVATE_SLUG}"])
        verdict = self._verdict(
            [
                {"severity": "nit", "summary": "rename x", "file": "a.py", "line": 9},
                {"severity": "nit", "summary": "rename y", "file": "b.py", "line": 2},
                {"severity": "minor", "summary": "log the retry", "file": "", "line": 0},
            ]
        )
        host = _FakeHost()

        outcomes = [publish_verdict_findings(verdict, host_kind="github", backend=host) for _ in range(2)]

        assert all(ACTION in outcome.blocked_reason for outcome in outcomes)
        assert not host.reviews
        assert not SendAudit.objects.filter(action=ACTION).exists()


class TestCommentChecks(_PublishBase):
    """Every body of the review passes ``review post-comment``'s checks, or the whole review is withheld, no DM."""

    @pytest.fixture(autouse=True)
    def _dms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.dms: list[str] = []
        monkeypatch.setattr("teatree.core.notify.notify_user", lambda text, **_kw: self.dms.append(text))

    def _assert_withheld(self, outcome: PublishOutcome, host: _FakeHost, check: str) -> None:
        assert not outcome.published
        assert not host.reviews
        assert check in outcome.blocked_reason
        assert not self.dms

    def test_several_anchored_findings_are_posted_one_per_inline_comment(self) -> None:
        self._allow_posting()
        verdict = self._verdict(
            [
                {"severity": "nit", "summary": "rename x", "file": "a.py", "line": 9},
                {"severity": "nit", "summary": "rename y", "file": "b.py", "line": 2},
            ],
            verdict="merge_safe",
        )
        host = _FakeHost()

        outcome = publish_verdict_findings(verdict, host_kind="github", backend=host)

        assert outcome.published
        assert [(c.path, c.line) for c in host.reviews[0].comments] == [("a.py", 9), ("b.py", 2)]

    def test_an_unbacked_claim_withholds_the_whole_review_naming_the_line(self) -> None:
        self._allow_posting()
        host = _FakeHost()
        verdict = self._verdict(
            [
                {"severity": "major", "summary": "the retry helper is missing", "file": "a.py", "line": 3},
                {"severity": "nit", "summary": "rename y", "file": "b.py", "line": 2},
            ]
        )
        outcome = publish_verdict_findings(verdict, host_kind="github", backend=host)
        self._assert_withheld(outcome, host, "the comment on a.py:3 — unbacked claim")

    def test_a_stakeholder_handle_is_withheld(self) -> None:
        self._allow_posting()
        host = _FakeHost()
        outcome = publish_verdict_findings(
            self._verdict([{"severity": "nit", "summary": "@bob flagged this loop"}]), host_kind="github", backend=host
        )
        self._assert_withheld(outcome, host, "comment bloat")

    def test_an_over_cap_body_is_withheld(self) -> None:
        self._allow_posting()
        host = _FakeHost()
        outcome = publish_verdict_findings(
            self._verdict([{"severity": "nit", "summary": _LONG_SUMMARY}]), host_kind="github", backend=host
        )
        self._assert_withheld(outcome, host, "colleague prose cap")

    def test_a_clean_review_is_submitted_once_and_audited(self) -> None:
        self._allow_posting()
        verdict = self._verdict(
            [{"severity": "nit", "summary": "rename x", "file": "a.py", "line": 9}], verdict="merge_safe"
        )
        host = _FakeHost()

        outcome = publish_verdict_findings(verdict, host_kind="github", backend=host)

        assert outcome.published
        assert len(host.reviews) == 1
        assert SendAudit.objects.exists()
        assert not self.dms

    def test_a_withheld_review_spends_no_approval_and_writes_no_send_audit(self) -> None:
        seed_forbidding_posture()
        ConfigSetting.objects.set_value("private_repos", [f"github.com/{_PRIVATE_SLUG}"])
        target = f"{_PRIVATE_SLUG}#4476"
        OnBehalfApproval.record(target, ACTION, "owner")
        host = _FakeHost()

        outcome = publish_verdict_findings(
            self._verdict([{"severity": "major", "summary": "the retry helper is missing"}]),
            host_kind="github",
            backend=host,
        )

        assert "unbacked claim" in outcome.blocked_reason
        assert not host.reviews
        assert OnBehalfApproval.objects.filter(target=target, action=ACTION, consumed_at__isnull=True).exists()
        assert not SendAudit.objects.exists()

    def test_a_three_paragraph_summary_is_posted_because_the_marker_is_not_counted(self) -> None:
        self._allow_posting()
        verdict = self._verdict([{"severity": "minor", "summary": "log the retry\n\nwith its count\n\nand cause"}])
        host = _FakeHost()

        outcome = publish_verdict_findings(verdict, host_kind="github", backend=host)

        assert outcome.published
        assert host.reviews[0].body.count("\n\n") == 2


class TestTodoAnchor(_PublishBase):
    """A blocker-shaped inline finding next to the author's own TODO is work they deferred — withheld, no DM."""

    @pytest.fixture(autouse=True)
    def _dms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.dms: list[str] = []
        monkeypatch.setattr("teatree.core.notify.notify_user", lambda text, **_kw: self.dms.append(text))

    def test_a_blocker_on_the_authors_todo_withholds_the_review_naming_the_line(self) -> None:
        self._allow_posting()
        host = _FakeHost()
        verdict = self._verdict(
            [{"severity": "major", "summary": "this loop must be bounded", "file": "a.py", "line": 11}]
        )

        outcome = publish_verdict_findings(verdict, host_kind="github", backend=host)

        assert not host.reviews
        assert "the comment on a.py:11 — TODO-anchored blocker: line 12" in outcome.blocked_reason
        assert not self.dms

    def test_a_blocker_on_a_file_whose_patch_the_forge_omitted_is_withheld(self) -> None:
        self._allow_posting()
        host = _OmittedPatchHost()
        verdict = self._verdict(
            [{"severity": "major", "summary": "this loop must be bounded", "file": "a.py", "line": 11}]
        )

        outcome = publish_verdict_findings(verdict, host_kind="github", backend=host)

        assert not host.reviews
        assert "the comment on a.py:11 — TODO anchor unreadable" in outcome.blocked_reason
        assert not self.dms

    def test_findings_that_read_as_no_blocker_never_read_the_diff(self) -> None:
        self._allow_posting()
        host = _FakeHost()
        verdict = self._verdict([{"severity": "nit", "summary": "rename the loop", "file": "a.py", "line": 12}])

        assert publish_verdict_findings(verdict, host_kind="github", backend=host).published
        assert host.diff_reads == 0

    def test_an_unreadable_diff_refuses_rather_than_skipping_the_check(self) -> None:
        self._allow_posting()
        host = _UnreadableDiffHost()
        verdict = self._verdict(
            [{"severity": "major", "summary": "this loop must be bounded", "file": "a.py", "line": 11}]
        )

        with pytest.raises(FindingsPublishError) as exc:
            publish_verdict_findings(verdict, host_kind="github", backend=host)

        assert "could not read the diff" in str(exc.value)
        assert not host.reviews
