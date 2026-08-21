"""A HOLD's findings reach the PR, or the refusal is loud (#4476).

The publish is idempotent by a hidden marker, routes through the leak /
send-proxy chokepoint, and is gated by the on-behalf pre-gate. Every path that
does NOT post names its own reason: the failure mode this forecloses is a
``findings_count`` with nothing behind it and nothing said about why.
"""

import pytest
from django.test import TestCase

from teatree.core.models import ConfigSetting, ReviewVerdict
from teatree.core.review.verdict_findings import marker_for
from teatree.core.review.verdict_findings_publish import ACTION, FindingsPublishError, publish_verdict_findings
from teatree.core.send_proxy import OutboundLeakError
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "e" * 40
_PRIVATE_SLUG = "democorp-engineering/widgets"


class _FakeHost:
    """The forge surface the publish uses — two methods, both recorded."""

    def __init__(self, comments: list[RawAPIDict] | None = None) -> None:
        self.comments: list[RawAPIDict] = list(comments or [])
        self.posted: list[RawAPIDict] = []

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


class _PublishBase(TestCase):
    """Isolate the on-behalf env so the DB store is the sole config tier."""

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_ON_BEHALF_POST_MODE", "T3_ON_BEHALF_AUTO_ACTIONS", "T3_BANNED_TERMS"):
            monkeypatch.delenv(env, raising=False)
        self.monkeypatch = monkeypatch

    @staticmethod
    def _verdict(findings: list[object] | None = None, *, slug: str = _PRIVATE_SLUG) -> ReviewVerdict:
        return ReviewVerdict.objects.create(
            slug=slug,
            pr_id=4476,
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="cold-reviewer",
            findings=findings if findings is not None else [{"severity": "blocker", "summary": "unbounded loop"}],
            blast_class="logic",
            gh_verify_result="green",
        )

    @staticmethod
    def _allow_posting() -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        ConfigSetting.objects.set_value("private_repos", [_PRIVATE_SLUG])


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
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        self.monkeypatch.setenv("T3_BANNED_TERMS", "democorp")
        verdict = self._verdict([{"severity": "blocker", "summary": "democorp secret in the log"}], slug="pub/repo")
        host = _FakeHost()

        with pytest.raises(OutboundLeakError):
            publish_verdict_findings(verdict, backend=host)
        assert not host.posted


class TestOnBehalfGate(_PublishBase):
    def test_the_shipped_default_withholds_the_post_and_names_both_ways_out(self) -> None:
        ConfigSetting.objects.set_value("private_repos", [_PRIVATE_SLUG])
        verdict = self._verdict()
        host = _FakeHost()

        outcome = publish_verdict_findings(verdict, backend=host)

        assert not outcome.published
        assert not host.posted
        assert ACTION in outcome.blocked_reason

    def test_the_block_is_reported_not_swallowed(self) -> None:
        ConfigSetting.objects.set_value("private_repos", [_PRIVATE_SLUG])
        outcome = publish_verdict_findings(self._verdict(), backend=_FakeHost())
        assert outcome.blocked_reason
        assert not outcome.skipped_existing
