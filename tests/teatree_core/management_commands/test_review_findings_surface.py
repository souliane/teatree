"""A recorded verdict's findings are readable through the CLI and reach the PR (#4476, #4968).

Before this, ``review status`` reported ``findings_count: 4`` and no surface
rendered the four: the author could not fix what they could not read, and a
later reviewer could not check the findings were addressed. These tests pin the
three halves of the fix — the read (``review findings`` / ``status --json``),
the publish (automatic on record, retryable through ``publish-findings``), and
the loud refusal that replaced the silent drop.
"""

import json
from io import StringIO
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.backend_protocols import PrReview
from teatree.core.modelkit.forge_readability import LiveHeadRead
from teatree.core.models import ConfigSetting, ReviewVerdict
from teatree.core.review.verdict_findings import marker_for
from teatree.types import RawAPIDict
from tests._send_gate import allow_forge_repos
from tests.teatree_core._on_behalf_gate_helpers import seed_permitting_posture

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "a" * 40
_SLUG = "democorp-engineering/widgets"
_URL = "https://github.com/democorp-engineering/widgets/pull/4476"
_FINDINGS = json.dumps(
    [
        {"severity": "blocker", "summary": "unbounded loop", "file": "a.py", "line": 9},
        {"severity": "nit", "summary": "rename x", "file": "b.py", "line": 0},
    ]
)


class _FakeHost:
    """A PR whose live head is the reviewed one, authored by a colleague unless told otherwise."""

    def __init__(self, *, author: str = "carol") -> None:
        self.posted: list[PrReview] = []
        self.author = author
        self.author_reads: list[str] = []

    def get_pr_author(self, *, pr_url: str) -> str:
        self.author_reads.append(pr_url)
        return self.author

    @staticmethod
    def current_user() -> str:
        return "owner-login"

    @staticmethod
    def fetch_live_head_sha(*, slug: str, pr_id: int) -> str:
        _ = slug, pr_id
        return _SHA

    def find_pr_review(self, *, repo: str, pr_iid: int, marker: str) -> bool:
        _ = repo, pr_iid
        return any(review.marker == marker for review in self.posted)

    def submit_pr_review(self, *, repo: str, pr_iid: int, review: PrReview) -> RawAPIDict:
        self.posted.append(review)
        return {"html_url": f"https://forge.test/{repo}/pull/{pr_iid}#review-{len(self.posted)}"}


class _FindingsSurfaceBase(TestCase):
    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch, configured_banned_term_registry: None) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_ON_BEHALF_AUTO_ACTIONS"):
            monkeypatch.delenv(env, raising=False)
        self.monkeypatch = monkeypatch
        self.host = _FakeHost()
        ConfigSetting.objects.set_value("private_repos", [f"github.com/{_SLUG}"])
        allow_forge_repos(_SLUG)

    def _allow_posting(self) -> None:
        seed_permitting_posture()

    def _record(self, *, forge: str = "github", **overrides: object) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "reviewed_sha": _SHA,
            "verdict": "hold",
            "reviewer_identity": "cold-reviewer",
            "gh_verify_result": "green",
            "blast_class": "logic",
            "findings_json": _FINDINGS,
        }
        kwargs.update(overrides)
        with (
            patch("teatree.core.backend_factory.code_host_from_overlay", return_value=self.host),
            patch("teatree.core.management.commands._review_impl.forge_for_repo_slug", return_value=forge),
        ):
            return cast("dict[str, object]", call_command("review", "record", "4476", _SLUG, **kwargs))

    @staticmethod
    def _status(*, head: str = _SHA, checks: str = "green", **kwargs: object) -> dict[str, object]:
        with (
            # `status` reads `live_head_read`, not `live_head_sha`: an UNREADABLE forge answer
            # has to stay distinguishable from a moved head (#4462), and the flattened
            # accessor cannot carry that. Patch the richer read the command actually calls.
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.live_head_read", return_value=LiveHeadRead.of(head)),
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.required_checks_status", return_value=checks),
        ):
            return cast("dict[str, object]", call_command("review", "status", _URL, **kwargs))


class TestFindingsAreReadable(_FindingsSurfaceBase):
    def test_status_reports_the_findings_not_only_their_count(self) -> None:
        self._record()
        result = self._status()
        findings = cast("list[dict[str, object]]", result["findings"])
        assert result["findings_count"] == 2
        assert [row["summary"] for row in findings] == ["unbounded loop", "rename x"]
        assert findings[0]["file"] == "a.py"
        assert findings[0]["line"] == 9

    def test_status_json_puts_the_full_record_on_stdout(self) -> None:
        self._record()
        out = StringIO()
        with (
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.live_head_read", return_value=LiveHeadRead.of(_SHA)),
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.required_checks_status", return_value="green"),
        ):
            call_command("review", "status", _URL, "--json", stdout=out)
        payload = json.loads(out.getvalue())
        assert payload["verdict"] == "hold"
        assert [row["summary"] for row in payload["findings"]] == ["unbounded loop", "rename x"]

    def test_findings_command_renders_every_finding(self) -> None:
        self._record()
        result = cast("dict[str, object]", call_command("review", "findings", _URL))
        assert result["findings_count"] == 2
        assert result["verdict"] == "hold"
        assert cast("list[dict[str, object]]", result["findings"])[1]["summary"] == "rename x"

    def test_findings_command_emits_json_on_stdout(self) -> None:
        self._record()
        out = StringIO()
        call_command("review", "findings", _URL, "--json", stdout=out)
        payload = json.loads(out.getvalue())
        assert payload["reviewer_identity"] == "cold-reviewer"
        assert len(payload["findings"]) == 2

    def test_findings_command_can_read_a_specific_reviewed_sha(self) -> None:
        self._record()
        result = cast("dict[str, object]", call_command("review", "findings", _URL, reviewed_sha=_SHA))
        assert result["reviewed_sha"] == _SHA

    def test_findings_command_says_so_when_nothing_is_recorded(self) -> None:
        result = cast("dict[str, object]", call_command("review", "findings", _URL))
        assert result["state"] == "no_verdict"
        assert result["findings_count"] == 0

    def test_findings_command_refuses_an_unparsable_url(self) -> None:
        with pytest.raises(SystemExit):
            call_command("review", "findings", "not-a-pr-url")


class TestFindingsReachThePr(_FindingsSurfaceBase):
    def test_recording_a_hold_posts_its_findings_to_the_pr(self) -> None:
        self._allow_posting()
        result = self._record()
        assert result["findings_published"]
        assert len(self.host.posted) == 1
        review = self.host.posted[0]
        assert [(c.path, c.line) for c in review.comments] == [("a.py", 9)]
        assert "rename x" in review.body

    def test_recording_on_an_own_pr_posts_nothing_and_says_self_review(self) -> None:
        self._allow_posting()
        self.host = _FakeHost(author="owner-login")
        result = self._record()
        assert not result["findings_published"]
        assert "self-review" in cast("str", result["findings_publish_note"])
        assert not self.host.posted

    def test_recording_reads_the_author_on_the_forge_that_hosts_the_repo(self) -> None:
        self._allow_posting()
        self._record(forge="gitlab")
        assert self.host.author_reads == [f"https://gitlab.com/{_SLUG}/-/merge_requests/4476"]

    def test_recording_on_a_repo_with_no_declared_forge_posts_nothing_and_says_why(self) -> None:
        self._allow_posting()
        result = self._record(forge="")
        assert not result["findings_published"]
        assert "forge hosting" in cast("str", result["findings_publish_note"])
        assert not self.host.author_reads
        assert not self.host.posted

    def test_publish_findings_does_not_post_a_second_copy(self) -> None:
        self._allow_posting()
        self._record()
        with patch("teatree.core.backend_factory.code_host_from_overlay", return_value=self.host):
            result = cast("dict[str, object]", call_command("review", "publish-findings", _URL))
        assert result["skipped_existing"]
        assert len(self.host.posted) == 1

    def test_publish_findings_backfills_a_verdict_recorded_while_blocked(self) -> None:
        self._record()
        assert not self.host.posted

        self._allow_posting()
        with patch("teatree.core.backend_factory.code_host_from_overlay", return_value=self.host):
            result = cast("dict[str, object]", call_command("review", "publish-findings", _URL))

        assert result["published"]
        assert len(self.host.posted) == 1
        verdict = ReviewVerdict.objects.get(slug=_SLUG, pr_id=4476)
        assert self.host.posted[0].marker == marker_for(verdict)

    def test_a_withheld_post_is_reported_on_the_record_result(self) -> None:
        result = self._record()
        assert result["recorded"]
        assert not result["findings_published"]
        assert "post_review_findings" in cast("str", result["findings_publish_note"])

    def test_publish_findings_reports_when_no_verdict_exists(self) -> None:
        result = cast("dict[str, object]", call_command("review", "publish-findings", _URL))
        assert not result["published"]
        assert "nothing to publish" in cast("str", result["note"])

    def test_a_recorded_verdict_survives_a_forge_failure(self) -> None:
        self._allow_posting()
        with patch(
            "teatree.core.management.commands._review_impl.publish_verdict_findings",
            side_effect=RuntimeError("forge down"),
        ):
            result = self._record()
        assert result["recorded"]
        assert not result["findings_published"]
        assert ReviewVerdict.objects.filter(slug=_SLUG, pr_id=4476).exists()


class TestPublishedFindingsPassTheCommentChecks(_FindingsSurfaceBase):
    """#4968: a published findings comment passes ``review post-comment``'s checks or is withheld."""

    def test_recording_a_verdict_with_an_unbacked_claim_withholds_it(self) -> None:
        self._allow_posting()
        findings = json.dumps(
            [{"severity": "minor", "summary": "the retry helper is missing", "file": "c.py", "line": 30}]
        )
        with patch("teatree.loop.sweep_on_demand._sweep_scanner_for_overlay", return_value=None):
            result = self._record(verdict="merge_safe", findings_json=findings)

        assert result["recorded"]
        assert not result["findings_published"]
        assert "unbacked claim" in cast("str", result["findings_publish_note"])
        assert not self.host.posted

    def test_publish_findings_reports_a_leak_block_instead_of_raising(self) -> None:
        self._allow_posting()
        ReviewVerdict.objects.create(
            slug="pub/repo",
            pr_id=4476,
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="cold-reviewer",
            findings=[{"severity": "blocker", "summary": "quartzbridge-bank secret in the log"}],
            blast_class="logic",
            gh_verify_result="green",
        )
        with patch("teatree.core.backend_factory.code_host_from_overlay", return_value=self.host):
            result = cast(
                "dict[str, object]",
                call_command("review", "publish-findings", "https://github.com/pub/repo/pull/4476"),
            )

        assert not result["published"]
        assert result["error"]
        assert not self.host.posted


class TestUnrenderableFindingsAreLoud(_FindingsSurfaceBase):
    def test_a_non_object_finding_is_refused_at_record_time(self) -> None:
        # A `record` refusal EXITS non-zero on every caller, not only from argv (#932):
        # an `{"error": …}` return prints and exits 0 for the in-process one.
        err = StringIO()
        with pytest.raises(SystemExit) as exc:
            self._record(findings_json='[{"severity": "nit", "summary": "ok"}, 7]', stderr=err)
        assert exc.value.code == 1
        assert "element 1 is int" in err.getvalue()
        assert not ReviewVerdict.objects.exists()

    def test_a_persisted_unrenderable_payload_refuses_rather_than_rendering_a_count(self) -> None:
        self._record()
        ReviewVerdict.objects.filter(slug=_SLUG, pr_id=4476).update(
            findings=[{"severity": "nit", "summary": "ok"}, "corrupt"]
        )
        with pytest.raises(Exception, match="not an object"):
            call_command("review", "findings", _URL)
