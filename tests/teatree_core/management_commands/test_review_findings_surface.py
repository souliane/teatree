"""A recorded HOLD's findings are readable through the CLI and reach the PR (#4476).

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

from teatree.core.modelkit.forge_readability import LiveHeadRead
from teatree.core.models import ConfigSetting, ReviewVerdict
from teatree.core.review.verdict_findings import marker_for
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "a" * 40
_SLUG = "democorp-engineering/widgets"
_URL = "https://github.com/democorp-engineering/widgets/pull/4476"
_FINDINGS = json.dumps(
    [
        {"severity": "blocker", "summary": "unbounded loop", "file": "a.py", "line": 9},
        {"severity": "nit", "summary": "rename x", "file": "b.py", "line": 2},
    ]
)


class _FakeHost:
    def __init__(self) -> None:
        self.comments: list[RawAPIDict] = []
        self.posted: list[RawAPIDict] = []

    def list_pr_comments(self, *, repo: str, pr_iid: int) -> list[RawAPIDict]:
        _ = repo, pr_iid
        return list(self.comments)

    def post_pr_comment(self, *, repo: str, pr_iid: int, body: str) -> RawAPIDict:
        self.posted.append({"repo": repo, "pr_iid": pr_iid, "body": body})
        self.comments.append({"body": body})
        return {"html_url": f"https://forge.test/{repo}/pull/{pr_iid}#note-{len(self.posted)}"}


class _FindingsSurfaceBase(TestCase):
    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_ON_BEHALF_POST_MODE", "T3_ON_BEHALF_AUTO_ACTIONS", "T3_BANNED_TERMS"):
            monkeypatch.delenv(env, raising=False)
        self.monkeypatch = monkeypatch
        self.host = _FakeHost()
        ConfigSetting.objects.set_value("private_repos", [_SLUG])

    def _allow_posting(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")

    def _record(self, **overrides: object) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "reviewed_sha": _SHA,
            "verdict": "hold",
            "reviewer_identity": "cold-reviewer",
            "gh_verify_result": "green",
            "blast_class": "logic",
            "findings_json": _FINDINGS,
        }
        kwargs.update(overrides)
        with patch("teatree.core.backend_factory.code_host_from_overlay", return_value=self.host):
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
        body = str(self.host.posted[0]["body"])
        assert "unbounded loop" in body
        assert "a.py:9" in body
        assert "rename x" in body

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
        assert marker_for(verdict) in str(self.host.posted[0]["body"])

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


class TestUnrenderableFindingsAreLoud(_FindingsSurfaceBase):
    def test_a_non_object_finding_is_refused_at_record_time(self) -> None:
        result = self._record(findings_json='[{"severity": "nit", "summary": "ok"}, 7]')
        assert not result["recorded"]
        assert "element 1 is int" in cast("str", result["error"])
        assert not ReviewVerdict.objects.exists()

    def test_a_persisted_unrenderable_payload_refuses_rather_than_rendering_a_count(self) -> None:
        self._record()
        ReviewVerdict.objects.filter(slug=_SLUG, pr_id=4476).update(
            findings=[{"severity": "nit", "summary": "ok"}, "corrupt"]
        )
        with pytest.raises(Exception, match="not an object"):
            call_command("review", "findings", _URL)
