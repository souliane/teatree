"""Tests for #2304: templates, never-render-empty, --body-file."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import _test_plan
from teatree.core.management.commands import _test_plan_render as _render
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}
_ISSUE_URL = "https://gitlab.com/org/repo/-/issues/8521"
_MOCK_OVERLAY_VALUE = next(iter(_MOCK_OVERLAY.values()))


def _local_side(workflows: dict) -> _render.SideState:
    return {"commits": {"client": "aabb"}, "workflows": workflows}


def _empty_side(*, env: str) -> _render.SideState:
    side: _render.SideState = {"commits": {}, "workflows": {}}
    if env == "dev":
        side["missing_on_dev"] = []
    return side


# ---------------------------------------------------------------------------
# TestBrowserClickFirstTemplate
# ---------------------------------------------------------------------------


class TestBrowserClickFirstTemplate(TestCase):
    def _state(self, *, steps: list[str] | None = None) -> _render.TestPlanState:
        return {
            "ticket": "8521",
            "title": "Login flow",
            "mrs": [],
            "dev": _empty_side(env="dev"),
            "local": _local_side(
                {
                    "Login": {
                        "video_md": "",
                        "image_md": [
                            "![s1](/uploads/s/s1.png)",
                            "![s2](/uploads/s/s2.png)",
                        ],
                    }
                }
            ),
            "steps": {"Login": steps or ["Open the app", "Click Login", "Expect dashboard"]},
            "template": "browser-click-first",
        }

    def test_renders_numbered_steps(self) -> None:
        body = _render.render_body(self._state())
        assert "1. Open the app" in body
        assert "2. Click Login" in body
        assert "3. Expect dashboard" in body

    def test_no_dev_local_table(self) -> None:
        body = _render.render_body(self._state())
        assert "| Dev | Local |" not in body

    def test_screenshots_inline_not_in_table(self) -> None:
        body = _render.render_body(self._state())
        assert "![s1](/uploads/s/s1.png)" in body
        assert "![s2](/uploads/s/s2.png)" in body

    def test_blocked_workflow_renders_blocked_marker(self) -> None:
        state = self._state()
        state["blocked_workflows"] = {"Checkout": "Not deployed yet"}
        body = _render.render_body(state)
        visible = body.split("-->")[-1]
        assert "Checkout" in visible
        assert "Not deployed yet" in visible


# ---------------------------------------------------------------------------
# TestLinkApiTemplate
# ---------------------------------------------------------------------------


class TestLinkApiTemplate(TestCase):
    def _state(self) -> _render.TestPlanState:
        return {
            "ticket": "8521",
            "title": "API check",
            "mrs": [],
            "dev": _empty_side(env="dev"),
            "local": _local_side(
                {
                    "Create user": {
                        "video_md": "",
                        "image_md": [],
                        "link_md": "[POST /users](https://gitlab.com/org/repo/-/issues/8521)",
                        "code_md": '```json\n{"id": 1}\n```',
                    }
                }
            ),
            "steps": {},
            "template": "link-api",
        }

    def test_renders_link(self) -> None:
        body = _render.render_body(self._state())
        assert "[POST /users]" in body

    def test_renders_code_block(self) -> None:
        body = _render.render_body(self._state())
        assert "```json" in body

    def test_no_dev_local_table(self) -> None:
        body = _render.render_body(self._state())
        assert "| Dev | Local |" not in body


# ---------------------------------------------------------------------------
# TestNeverEmptyRender
# ---------------------------------------------------------------------------


class TestNeverEmptyRender(TestCase):
    def test_raises_on_empty_state(self) -> None:
        state: _render.TestPlanState = {
            "ticket": "8521",
            "title": "Empty",
            "mrs": [],
            "dev": _empty_side(env="dev"),
            "local": _empty_side(env="local"),
            "steps": {},
        }
        with pytest.raises(_render.TestPlanValidationError, match="empty"):
            _render.render_body(state)


# ---------------------------------------------------------------------------
# TestBodyFile — command-level: curated body posted directly, no upload
# ---------------------------------------------------------------------------


class TestBodyFile(TestCase):
    def _ticket(self) -> MagicMock:
        ticket = MagicMock()
        ticket.issue_url = _ISSUE_URL
        ticket.ticket_number = "8521"
        return ticket

    def _patch_host(self) -> MagicMock:
        host = MagicMock()
        host.repo_for_issue_url.return_value = "org/repo"
        host.list_issue_comments.return_value = []
        host.post_issue_comment.return_value = {"id": 42}
        return host

    def test_body_file_posts_content_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body_path = Path(tmp) / "plan.md"
            body_path.write_text("<!-- t3-e2e-evidence ticket=8521 -->\n## Test Plan\n\nSome steps.\n")
            host = self._patch_host()
            with (
                patch("teatree.core.management.commands._test_plan.code_host_from_overlay", return_value=host),
                patch("teatree.core.management.commands._test_plan._resolve_worktree_or_none", return_value=None),
                patch("teatree.core.models.Ticket.objects.resolve", return_value=self._ticket()),
                patch(
                    "teatree.core.management.commands._test_plan.require_on_behalf_approval",
                    side_effect=lambda **kw: kw["publish"](),
                ),
                patch("teatree.core.management.commands._test_plan.on_behalf_block_message", return_value=""),
                patch("teatree.core.management.commands._test_plan.notify_user_on_behalf_post"),
                patch("teatree.core.overlay_loader.get_overlay", return_value=_MOCK_OVERLAY_VALUE),
            ):
                call_command("e2e", "post-test-plan", ticket="8521", body_file=str(body_path))
            host.upload_file.assert_not_called()
            posted_body = host.post_issue_comment.call_args[1]["body"]
            assert "## Test Plan" in posted_body

    def test_empty_body_file_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body_path = Path(tmp) / "empty.md"
            body_path.write_text("")
            with (
                pytest.raises(SystemExit) as exc_info,
                patch(
                    "teatree.core.management.commands._test_plan.code_host_from_overlay",
                    return_value=self._patch_host(),
                ),
                patch("teatree.core.overlay_loader.get_overlay", return_value=_MOCK_OVERLAY_VALUE),
            ):
                call_command("e2e", "post-test-plan", ticket="8521", body_file=str(body_path))
            assert exc_info.value.code != 0

    def test_body_file_and_manifest_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body_path = Path(tmp) / "plan.md"
            body_path.write_text("## Plan\n")
            with (
                pytest.raises(SystemExit) as exc_info,
                patch(
                    "teatree.core.management.commands._test_plan.code_host_from_overlay",
                    return_value=self._patch_host(),
                ),
                patch("teatree.core.overlay_loader.get_overlay", return_value=_MOCK_OVERLAY_VALUE),
            ):
                call_command(
                    "e2e",
                    "post-test-plan",
                    ticket="8521",
                    body_file=str(body_path),
                    manifest='{"workflows":[]}',
                )
            assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# TestTemplateThroughManifest — the production path selects the template
# ---------------------------------------------------------------------------


_BROWSER_MANIFEST = json.dumps(
    {
        "ticket": "8521",
        "template": "browser-click-first",
        "local": {"commits": {"client": "aabb"}},
        "workflows": [{"workflow": "Login", "steps": ["Open the app", "Click Login"]}],
    }
)


class TestTemplateThroughManifest(TestCase):
    """``parse_manifest`` reads ``template`` and ``merge_state`` writes it into state."""

    def _browser_manifest(self) -> _render.TestPlanManifest:
        return _render.parse_manifest(_BROWSER_MANIFEST)

    def test_parse_manifest_reads_template(self) -> None:
        assert self._browser_manifest().template == "browser-click-first"

    def test_parse_manifest_defaults_template_to_capture_matrix(self) -> None:
        manifest = _render.parse_manifest(json.dumps({"ticket": "8521", "local": {}, "workflows": [{"workflow": "X"}]}))
        assert manifest.template == "capture-matrix"

    def test_parse_manifest_rejects_unknown_template(self) -> None:
        with pytest.raises(_render.TestPlanValidationError, match="template"):
            _render.parse_manifest(json.dumps({"template": "bogus", "local": {}, "workflows": [{"workflow": "X"}]}))

    def test_merge_state_sets_template_from_manifest(self) -> None:
        merged = _render.merge_state(
            _render.empty_state(ticket="8521", title="t"),
            manifest=self._browser_manifest(),
            title="Login flow",
            embeds={"dev": {}, "local": {"Login": {"video_md": "", "image_md": ["![s](/uploads/s/s.png)"]}}},
        )
        assert merged["template"] == "browser-click-first"

    def test_browser_template_body_via_production_path(self) -> None:
        merged = _render.merge_state(
            _render.empty_state(ticket="8521", title="t"),
            manifest=self._browser_manifest(),
            title="Login flow",
            embeds={"dev": {}, "local": {"Login": {"video_md": "", "image_md": ["![s](/uploads/s/s.png)"]}}},
        )
        body = _render.render_body(merged)
        assert "| Dev | Local |" not in body
        assert "1. Open the app" in body
        assert "![s](/uploads/s/s.png)" in body


# ---------------------------------------------------------------------------
# TestTemplateRoundTrip — template + fields survive a re-read of the state blob
# ---------------------------------------------------------------------------


class TestTemplateRoundTrip(TestCase):
    """A second ``post-test-plan`` re-reads the blob; new fields must survive."""

    def _seeded_state(self) -> _render.TestPlanState:
        return {
            "ticket": "8521",
            "title": "Login flow",
            "mrs": [],
            "dev": _empty_side(env="dev"),
            "local": _local_side(
                {
                    "Create user": {
                        "video_md": "",
                        "image_md": [],
                        "link_md": "[POST /users](https://gitlab.com/org/repo/-/issues/8521)",
                        "code_md": '```json\n{"id": 1}\n```',
                    }
                }
            ),
            "steps": {},
            "template": "link-api",
            "blocked_workflows": {"Checkout": "Not deployed yet"},
        }

    def _reread(self, state: _render.TestPlanState) -> _render.TestPlanState:
        return _render.parse_state_blob(_render.render_body(state))

    def test_template_survives_round_trip(self) -> None:
        assert self._reread(self._seeded_state()).get("template") == "link-api"

    def test_blocked_workflows_survive_round_trip(self) -> None:
        assert self._reread(self._seeded_state()).get("blocked_workflows") == {"Checkout": "Not deployed yet"}

    def test_link_md_and_code_md_survive_round_trip(self) -> None:
        embed = self._reread(self._seeded_state())["local"]["workflows"]["Create user"]
        assert embed.get("link_md") == "[POST /users](https://gitlab.com/org/repo/-/issues/8521)"
        assert embed.get("code_md") == '```json\n{"id": 1}\n```'

    def test_re_render_after_round_trip_stays_link_api(self) -> None:
        reread = self._reread(self._seeded_state())
        body = _render.render_body(reread)
        assert "| Dev | Local |" not in body
        assert "[POST /users]" in body


# ---------------------------------------------------------------------------
# TestCaptureMatrixRendersBlocked — default template shows blocked workflows
# ---------------------------------------------------------------------------


class TestCaptureMatrixRendersBlocked(TestCase):
    def test_capture_matrix_renders_blocked_workflow(self) -> None:
        state: _render.TestPlanState = {
            "ticket": "8521",
            "title": "Login flow",
            "mrs": [],
            "dev": _empty_side(env="dev"),
            "local": _local_side({"Login": {"video_md": "", "image_md": ["![s](/uploads/s/s.png)"]}}),
            "steps": {},
            "blocked_workflows": {"Checkout": "Not deployed yet"},
        }
        body = _render.render_body(state)
        visible = body.split("-->")[-1]
        assert "| Dev | Local |" in visible
        assert "Checkout" in visible
        assert "Not deployed yet" in visible


# ---------------------------------------------------------------------------
# TestTemplateFlag — the --template CLI flag threads into the post
# ---------------------------------------------------------------------------


class TestTemplateFlag(TestCase):
    def test_template_flag_overrides_manifest_default(self) -> None:
        flags = _test_plan.TestPlanFlags(
            ticket="8521",
            manifest=json.dumps({"ticket": "8521", "local": {}, "workflows": [{"workflow": "Login"}]}),
            template="browser-click-first",
        )
        with patch("teatree.core.management.commands._test_plan._resolve_worktree_or_none", return_value=None):
            ticket = MagicMock()
            ticket.issue_url = _ISSUE_URL
            ticket.ticket_number = "8521"
            with patch("teatree.core.models.Ticket.objects.resolve", return_value=ticket):
                post = _test_plan.build_validated_post(flags)
        assert post.manifest.template == "browser-click-first"
