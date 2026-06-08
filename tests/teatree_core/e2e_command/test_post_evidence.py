"""Tests for ``t3 <overlay> e2e post-evidence`` (teatree #272, #2165).

The one-note-per-ticket evidence model: a single GitLab note per ticket that
renders a side-by-side ``Dev | Local`` test plan and accumulates environment
columns across runs via a hidden machine-readable state blob.

The pure-builder half exercises the manifest parse, the merge over prior state,
the side-by-side render (videos row, screenshot pairs, em-dash cells, the
dev-gap line, per-repo commit provenance, MR links), and the splice that adds a
dev column while preserving a frozen local column.

The relative-embed half asserts the body embeds the claimable relative
``/uploads/<secret>/<file>`` reference, never the absolute ``/-/project/`` or
any ``https://`` upload URL (the #2165 regression).

The hard-fail half asserts the validators refuse bad evidence with no host side
effect; the media-gate half asserts a non-rendering upload aborts the post; the
on-behalf half asserts the gate stays in front of the post.
"""

import json
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.backend_protocols import UploadVerification
from teatree.core.management.commands import _e2e_evidence as _evidence
from teatree.core.management.commands import _e2e_evidence_render as _render
from teatree.core.management.commands import e2e as e2e_command
from teatree.core.overlay import OverlayMetadata
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}
_ISSUE_URL = "https://gitlab.com/org/repo/-/issues/8521"


def _write_png(path: Path, payload: bytes) -> str:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + payload)
    return str(path)


def _write_webm(path: Path, payload: bytes) -> str:
    path.write_bytes(b"\x1a\x45\xdf\xa3" + payload)
    return str(path)


# --- pure builder: render + merge + parse -----------------------------------


class TestRenderBody:
    """The side-by-side Dev | Local render is a pure function of the merged state."""

    def _embedded(self, *, video: str = "", images: tuple[str, ...] = ()) -> _render.WorkflowEmbed:
        return {"video_md": video, "image_md": list(images)}

    def _state(
        self,
        *,
        dev: _render.SideState | None = None,
        local: _render.SideState | None = None,
        mrs: list[str] | None = None,
        steps: dict[str, list[str]] | None = None,
    ) -> _render.EvidenceState:
        default_mrs = [
            "https://gitlab.com/org/client/-/merge_requests/6331",
            "https://gitlab.com/org/product/-/merge_requests/7585",
        ]
        return {
            "ticket": "8521",
            "title": "My feature",
            "mrs": default_mrs if mrs is None else mrs,
            "dev": dev if dev is not None else {"commits": {}, "missing_on_dev": [], "workflows": {}},
            "local": local if local is not None else {"commits": {}, "workflows": {}},
            "steps": steps or {},
        }

    def test_header_has_marker_data_blob_title_and_mr_links(self) -> None:
        state = self._state(
            local={"commits": {"client": "aaaa", "product": "bbbb"}, "workflows": {"Login": self._embedded()}},
        )
        body = _evidence.render_body(state)
        assert "<!-- t3-e2e-evidence ticket=8521 -->" in body
        assert "<!-- t3-e2e-data " in body
        assert "## E2E Evidence — My feature" in body
        # Multi-repo MR links, terse repo!num labels.
        assert "Repos & MRs: [client!6331](" in body
        assert "[product!7585](" in body
        # Per-repo commit provenance for the tested side.
        assert "Local tested: client `aaaa`, product `bbbb`" in body

    def test_side_by_side_table_pairs_dev_left_local_right(self) -> None:
        state = self._state(
            dev={
                "commits": {"client": "ddee"},
                "missing_on_dev": [],
                "workflows": {
                    "Login": self._embedded(video="![v](/uploads/s/dev.webm)", images=("![i](/uploads/s/d1.png)",))
                },
            },
            local={
                "commits": {"client": "aabb"},
                "workflows": {
                    "Login": self._embedded(video="![v](/uploads/s/loc.webm)", images=("![i](/uploads/s/l1.png)",))
                },
            },
        )
        body = _evidence.render_body(state)
        assert "### Login" in body
        assert "| Dev | Local |" in body
        # Video row first: dev video left, local video right.
        assert "| ![v](/uploads/s/dev.webm) | ![v](/uploads/s/loc.webm) |" in body
        # Screenshot pair row.
        assert "| ![i](/uploads/s/d1.png) | ![i](/uploads/s/l1.png) |" in body
        assert "Dev deployed: client `ddee`" in body

    def test_missing_side_renders_emdash_cells(self) -> None:
        # Local captured, dev not yet deployed → dev column is all em-dashes.
        state = self._state(
            local={
                "commits": {"client": "aabb"},
                "workflows": {
                    "Login": self._embedded(video="![v](/uploads/s/loc.webm)", images=("![i](/uploads/s/l1.png)",))
                },
            },
        )
        body = _evidence.render_body(state)
        assert "| — | ![v](/uploads/s/loc.webm) |" in body
        assert "| — | ![i](/uploads/s/l1.png) |" in body

    def test_dev_gap_reconciliation_line_renders(self) -> None:
        state = self._state(
            dev={
                "commits": {"client": "ddee"},
                "missing_on_dev": ["client!6331 (unmerged)", "product!7585 (draft)"],
                "workflows": {"Login": self._embedded()},
            },
        )
        body = _evidence.render_body(state)
        assert "⚠️ Not yet on dev: client!6331 (unmerged), product!7585 (draft) — expected gap." in body

    def test_workflow_with_no_video_omits_video_cell_content(self) -> None:
        state = self._state(
            local={"commits": {}, "workflows": {"Search": self._embedded(images=("![i](/uploads/s/x.png)",))}},
        )
        body = _evidence.render_body(state)
        # No local video → the video cell is the em-dash placeholder, not blank.
        assert "| — | — |" in body  # dev side absent + local video absent
        assert "| — | ![i](/uploads/s/x.png) |" in body

    def test_mrs_line_omitted_when_no_mrs(self) -> None:
        state = self._state(mrs=[], local={"commits": {}, "workflows": {"Wf": self._embedded(images=("![i](u)",))}})
        body = _evidence.render_body(state)
        assert "Repos & MRs:" not in body

    def test_test_plan_steps_render_numbered_above_the_table(self) -> None:
        state = self._state(
            local={"commits": {}, "workflows": {"Login": self._embedded(images=("![i](/uploads/s/l1.png)",))}},
            steps={"Login": ["Open the app", "Click the Login button", "Expect the dashboard"]},
        )
        body = _evidence.render_body(state)
        assert "**How to test:**" in body
        assert "1. Open the app" in body
        assert "2. Click the Login button" in body
        assert "3. Expect the dashboard" in body
        # The numbered plan renders ABOVE the comparison table for that workflow.
        how_to = body.index("**How to test:**")
        table = body.index("| Dev | Local |")
        assert how_to < table, "the test plan must render above the Dev | Local table"
        # And it sits under the workflow heading.
        assert body.index("### Login") < how_to

    def test_workflow_without_steps_omits_the_test_plan_block(self) -> None:
        # Back-compat: a workflow with no steps renders no test-plan block.
        state = self._state(
            local={"commits": {}, "workflows": {"Search": self._embedded(images=("![i](u)",))}},
            steps={},
        )
        body = _evidence.render_body(state)
        assert "**How to test:**" not in body


class TestMergeState:
    """The merge over prior state freezes the side this run does not carry."""

    def _local_manifest(self) -> _evidence.EvidenceManifest:
        return _evidence.EvidenceManifest(
            ticket="8521",
            mrs=("https://gitlab.com/org/client/-/merge_requests/6331",),
            dev=_evidence.SideManifest(present=False),
            local=_evidence.SideManifest(present=True, commits={"client": "aabb"}),
        )

    def _dev_manifest(self) -> _evidence.EvidenceManifest:
        return _evidence.EvidenceManifest(
            ticket="8521",
            mrs=(),
            dev=_evidence.SideManifest(present=True, commits={"client": "ddee"}, missing_on_dev=()),
            local=_evidence.SideManifest(present=False),
        )

    def test_dev_only_run_preserves_existing_local_column(self) -> None:
        prior: _render.EvidenceState = {
            "ticket": "8521",
            "title": "t",
            "mrs": [],
            "dev": {"commits": {}, "missing_on_dev": ["client!6331 (unmerged)"], "workflows": {}},
            "local": {
                "commits": {"client": "aabb"},
                "workflows": {"Login": {"video_md": "![v](/uploads/s/l.webm)", "image_md": []}},
            },
            "steps": {},
        }
        merged = _evidence.merge_state(
            prior,
            manifest=self._dev_manifest(),
            title="t",
            embeds={"dev": {"Login": {"video_md": "![v](/uploads/s/dev.webm)", "image_md": []}}, "local": {}},
        )
        # Dev overwritten (new commit, gap cleared, new captures).
        assert merged["dev"]["commits"] == {"client": "ddee"}
        assert merged["dev"]["missing_on_dev"] == []
        assert merged["dev"]["workflows"]["Login"]["video_md"] == "![v](/uploads/s/dev.webm)"
        # Local frozen exactly as it was.
        assert merged["local"]["commits"] == {"client": "aabb"}
        assert merged["local"]["workflows"]["Login"]["video_md"] == "![v](/uploads/s/l.webm)"

    def test_steps_less_rerun_preserves_prior_steps(self) -> None:
        # A workflow's steps were recorded on a prior run; a later run that omits
        # steps must NOT erase them (workflow-level, persisted across re-renders).
        prior: _render.EvidenceState = {
            "ticket": "8521",
            "title": "t",
            "mrs": [],
            "dev": {"commits": {}, "missing_on_dev": [], "workflows": {}},
            "local": {"commits": {"client": "aabb"}, "workflows": {}},
            "steps": {"Login": ["Open the app", "Click Login"]},
        }
        merged = _evidence.merge_state(
            prior,
            manifest=self._dev_manifest(),  # carries no steps
            title="t",
            embeds={"dev": {}, "local": {}},
        )
        assert merged["steps"]["Login"] == ["Open the app", "Click Login"]

    def test_steps_in_this_run_overwrite_prior_steps_for_that_workflow(self) -> None:
        prior = _render.empty_state(ticket="8521", title="t")
        prior["steps"] = {"Login": ["old step"]}
        manifest = _evidence.EvidenceManifest(
            ticket="8521",
            mrs=(),
            dev=_evidence.SideManifest(present=False),
            local=_evidence.SideManifest(present=True, commits={"client": "aabb"}),
            steps={"Login": ("new step 1", "new step 2")},
        )
        merged = _evidence.merge_state(prior, manifest=manifest, title="t", embeds={"dev": {}, "local": {}})
        assert merged["steps"]["Login"] == ["new step 1", "new step 2"]

    def test_local_only_run_over_empty_prior_leaves_dev_empty(self) -> None:
        merged = _evidence.merge_state(
            _render.empty_state(ticket="8521", title="t"),
            manifest=self._local_manifest(),
            title="t",
            embeds={"dev": {}, "local": {"Login": {"video_md": "", "image_md": ["![i](/uploads/s/x.png)"]}}},
        )
        assert merged["local"]["commits"] == {"client": "aabb"}
        assert merged["dev"]["workflows"] == {}

    def test_add_dev_section_preserves_then_renders_both(self) -> None:
        # local first → render → recover state → dev run merges → both columns render.
        local_state = _evidence.merge_state(
            _render.empty_state(ticket="8521", title="My feature"),
            manifest=self._local_manifest(),
            title="My feature",
            embeds={
                "dev": {},
                "local": {"Login": {"video_md": "![v](/uploads/s/l.webm)", "image_md": ["![i](/uploads/s/l1.png)"]}},
            },
        )
        local_state["ticket"] = "8521"
        body_after_local = _evidence.render_body(local_state)
        recovered = _evidence.parse_state_blob(body_after_local)

        dev_state = _evidence.merge_state(
            recovered,
            manifest=self._dev_manifest(),
            title="My feature",
            embeds={
                "dev": {"Login": {"video_md": "![v](/uploads/s/d.webm)", "image_md": ["![i](/uploads/s/d1.png)"]}},
                "local": {},
            },
        )
        dev_state["ticket"] = "8521"
        final = _evidence.render_body(dev_state)
        # Both columns are present and paired.
        assert "| ![v](/uploads/s/d.webm) | ![v](/uploads/s/l.webm) |" in final
        assert "| ![i](/uploads/s/d1.png) | ![i](/uploads/s/l1.png) |" in final
        # Local survived the dev-only merge untouched.
        assert "Local tested: client `aabb`" in final


class TestParseManifest:
    """The manifest validator: shape, per-file existence, media kind."""

    def _manifest(self, tmp_path: Path, *, video: str | None, images: list[str]) -> str:
        return json.dumps(
            {
                "ticket": "8521",
                "mrs": ["https://gitlab.com/org/client/-/merge_requests/6331"],
                "local": {"commits": {"client": "aabb"}},
                "workflows": [{"workflow": "Login", "local": {"video": video, "images": images}}],
            },
        )

    def test_parses_valid_local_manifest(self, tmp_path: Path) -> None:
        img = _write_png(tmp_path / "a.png", b"A")
        vid = _write_webm(tmp_path / "v.webm", b"V")
        manifest = self._manifest(tmp_path, video=vid, images=[img])
        parsed = _evidence.parse_manifest(manifest)
        assert parsed.ticket == "8521"
        assert parsed.local.present is True
        assert parsed.dev.present is False
        wf = parsed.local.workflows["Login"]
        assert wf.video is not None
        assert len(wf.images) == 1

    def test_rejects_invalid_json(self) -> None:
        with pytest.raises(_evidence.EvidenceValidationError, match="not valid JSON"):
            _evidence.parse_manifest("{not json")

    def test_rejects_missing_workflows(self) -> None:
        with pytest.raises(_evidence.EvidenceValidationError, match="workflows"):
            _evidence.parse_manifest(json.dumps({"ticket": "8521", "local": {}}))

    def test_rejects_missing_artifact_file(self, tmp_path: Path) -> None:
        manifest = self._manifest(tmp_path, video=None, images=[str(tmp_path / "absent.png")])
        with pytest.raises(_evidence.EvidenceValidationError, match="not found"):
            _evidence.parse_manifest(manifest)

    def test_rejects_wrong_media_kind_for_video_slot(self, tmp_path: Path) -> None:
        # A .png handed to the video slot must be rejected.
        png = _write_png(tmp_path / "still.png", b"X")
        manifest = self._manifest(tmp_path, video=png, images=[_write_png(tmp_path / "ok.png", b"Y")])
        with pytest.raises(_evidence.EvidenceValidationError, match="not a recognised video"):
            _evidence.parse_manifest(manifest)

    def test_rejects_when_no_side_carries_captures(self, tmp_path: Path) -> None:
        manifest = json.dumps(
            {"ticket": "8521", "workflows": [{"workflow": "Login"}]},
        )
        with pytest.raises(_evidence.EvidenceValidationError, match="no 'dev' or 'local'"):
            _evidence.parse_manifest(manifest)

    def test_parses_workflow_level_steps(self, tmp_path: Path) -> None:
        img = _write_png(tmp_path / "a.png", b"A")
        manifest = json.dumps(
            {
                "ticket": "8521",
                "local": {"commits": {"client": "aabb"}},
                "workflows": [
                    {
                        "workflow": "Login",
                        "steps": ["Open the app", "Click Login", "Expect the dashboard"],
                        "local": {"images": [img]},
                    },
                    {"workflow": "Search", "local": {"images": [img]}},  # no steps → absent from the map
                ],
            },
        )
        parsed = _evidence.parse_manifest(manifest)
        assert parsed.steps["Login"] == ("Open the app", "Click Login", "Expect the dashboard")
        assert "Search" not in parsed.steps


class TestMrLabel:
    """The MR link rendering is a pure helper."""

    def test_gitlab_mr_renders_repo_bang_num(self) -> None:
        line = _evidence.render_mrs_line(("https://gitlab.com/grp/sub/client/-/merge_requests/6331",))
        assert line == "Repos & MRs: [client!6331](https://gitlab.com/grp/sub/client/-/merge_requests/6331)"

    def test_github_pr_renders_repo_hash_num(self) -> None:
        line = _evidence.render_mrs_line(("https://github.com/owner/product/pull/7585",))
        assert line == "Repos & MRs: [product#7585](https://github.com/owner/product/pull/7585)"

    def test_non_url_ref_shown_verbatim(self) -> None:
        line = _evidence.render_mrs_line(("client!6331",))
        assert line == "Repos & MRs: client!6331"


# --- command + host integration ---------------------------------------------


class _EvidenceTestBase(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        self._tmp = tmp_path

    @pytest.fixture(autouse=True)
    def _no_on_behalf_gate(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate  # noqa: PLC0415

        disable_on_behalf_gate(tmp_path_factory, monkeypatch)

    def _patch_host(self, host: MagicMock) -> None:
        self._monkeypatch.setattr(e2e_command, "code_host_from_overlay", lambda: host)
        self._monkeypatch.setattr(
            _evidence,
            "resolve_worktree",
            MagicMock(side_effect=_evidence.WorktreeNotFoundError("none")),
        )

    def _ticket(self) -> None:
        from teatree.core.models import Ticket  # noqa: PLC0415

        Ticket.objects.create(overlay="test", issue_url=_ISSUE_URL)

    def _local_manifest(self) -> str:
        img = _write_png(self._tmp / "step1.png", b"A")
        vid = _write_webm(self._tmp / "run.webm", b"V")
        return json.dumps(
            {
                "ticket": "8521",
                "mrs": ["https://gitlab.com/org/client/-/merge_requests/6331"],
                "local": {"commits": {"client": "aabb"}},
                "workflows": [{"workflow": "Login", "local": {"video": vid, "images": [img]}}],
            },
        )


class TestCreateAndRelativeEmbed(_EvidenceTestBase):
    """A first run creates the note and embeds the RELATIVE upload reference (#2165)."""

    def _run(self, host: MagicMock, **kwargs: object) -> dict[str, object]:
        self._patch_host(host)
        host.upload_file.return_value = {"full_path": "/-/project/9/uploads/deadbeef/x.png"}
        # The existence gate passes; the embed is the RELATIVE /uploads ref GitLab
        # claims on save — never the absolute /-/project or https:// form (#2165).
        host.verify_upload.return_value = UploadVerification(ok=True, embed_url="/uploads/deadbeef/x.png")
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            return cast("dict[str, object]", call_command("e2e", "post-evidence", **kwargs))

    def test_creates_note_with_relative_embed(self) -> None:
        self._ticket()
        host = MagicMock()
        host.list_issue_comments.return_value = []
        host.post_issue_comment.return_value = {"id": 77, "web_url": "u"}

        result = self._run(host, ticket=_ISSUE_URL, manifest=self._local_manifest())

        assert result["action"] == "created"
        assert result["comment_id"] == 77
        assert result["envs"] == ["local"]
        host.post_issue_comment.assert_called_once()
        host.update_issue_comment.assert_not_called()
        body = host.post_issue_comment.call_args.kwargs["body"]
        assert "<!-- t3-e2e-evidence ticket=8521 -->" in body
        # The body embeds the RELATIVE /uploads ref, never the absolute forms.
        assert "](/uploads/deadbeef/x.png)" in body
        assert "/-/project/" not in body
        assert "https://gitlab.com/-/project/" not in body
        host.verify_upload.assert_called()

    def test_updates_existing_note_in_place(self) -> None:
        self._ticket()
        host = MagicMock()
        prior_state = {
            "ticket": "8521",
            "title": "t",
            "mrs": [],
            "dev": {"commits": {}, "missing_on_dev": [], "workflows": {}},
            "local": {
                "commits": {"client": "old"},
                "workflows": {"Old": {"video_md": "", "image_md": ["![i](/uploads/s/o.png)"]}},
            },
        }
        marker = "<!-- t3-e2e-evidence ticket=8521 -->"
        blob = "<!-- t3-e2e-data " + json.dumps(prior_state, separators=(",", ":"), sort_keys=True) + " -->"
        host.list_issue_comments.return_value = [{"id": 33, "body": f"{marker}\n{blob}\n## E2E Evidence — t\n"}]
        host.update_issue_comment.return_value = {"id": 33, "web_url": "u"}

        result = self._run(host, ticket=_ISSUE_URL, manifest=self._local_manifest())

        assert result["action"] == "updated"
        assert result["comment_id"] == 33
        host.update_issue_comment.assert_called_once()
        host.post_issue_comment.assert_not_called()


class TestUploadLandsOnNoteProject(_EvidenceTestBase):
    """Artifacts upload to the SAME project the note is created on (multi-repo manifest).

    Live-run bug: the note was created on the ticket's project but every
    artifact uploaded to the manifest's *second* repo (the CI/product project),
    whose ``/uploads/<secret>/<file>`` namespace the ticket's note cannot serve
    → every embedded image 404s. The fix uploads to the project that owns the
    issue URL the note posts on, regardless of how many repos the manifest
    references. The MR links in the body are just links; they must NOT influence
    the upload target.
    """

    _NOTE_PROJECT = "org/repo"  # the project that owns _ISSUE_URL (the ticket's project)
    _SECOND_REPO = "org/product"  # the manifest's second repo / overlay CI project

    class _CiProjectMeta(OverlayMetadata):
        """Metadata whose CI project path is the manifest's SECOND repo."""

        def get_ci_project_path(self) -> str:
            return "org/product"

    class _CiProjectOverlay(CommandOverlay):
        """Overlay whose CI project path is a DIFFERENT project from the note's.

        Mirrors the live failure: ``get_ci_project_path`` resolves to the second
        repo, so the pre-fix code uploads artifacts there even though the note is
        created on the ticket's project — this test goes RED.
        """

        def __init__(self) -> None:
            self.metadata = TestUploadLandsOnNoteProject._CiProjectMeta()

    def _multi_repo_manifest(self) -> str:
        """A manifest carrying TWO repos, the second matching the CI project."""
        img = _write_png(self._tmp / "step1.png", b"A")
        vid = _write_webm(self._tmp / "run.webm", b"V")
        return json.dumps(
            {
                "ticket": "8521",
                "mrs": [
                    f"https://gitlab.com/{self._NOTE_PROJECT}/-/merge_requests/6331",
                    f"https://gitlab.com/{self._SECOND_REPO}/-/merge_requests/7585",
                ],
                "local": {"commits": {"repo": "aabb", "product": "ccdd"}},
                "workflows": [{"workflow": "Login", "local": {"video": vid, "images": [img]}}],
            },
        )

    def test_upload_project_is_the_notes_project_not_the_second_repo(self) -> None:
        self._ticket()
        host = MagicMock()
        host.list_issue_comments.return_value = []
        host.post_issue_comment.return_value = {"id": 77, "web_url": "u"}
        host.upload_file.return_value = {"full_path": "/-/project/9/uploads/deadbeef/x.png"}
        host.verify_upload.return_value = UploadVerification(ok=True, embed_url="/uploads/deadbeef/x.png")
        # The host resolves the note's own project slug from the issue URL.
        host.repo_for_issue_url.return_value = self._NOTE_PROJECT
        self._patch_host(host)

        overlay = {"test": self._CiProjectOverlay()}
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=overlay):
            call_command("e2e", "post-evidence", ticket=_ISSUE_URL, manifest=self._multi_repo_manifest())

        # Every upload must target the project that owns the note, NEVER the
        # manifest's second repo / CI project.
        assert host.upload_file.call_count >= 1
        for call in host.upload_file.call_args_list:
            assert call.kwargs["repo"] == self._NOTE_PROJECT, (
                f"upload landed on {call.kwargs['repo']!r}, expected the note's project {self._NOTE_PROJECT!r}"
            )
            assert call.kwargs["repo"] != self._SECOND_REPO
        for call in host.verify_upload.call_args_list:
            assert call.kwargs["repo"] == self._NOTE_PROJECT


class TestMediaRenderGate(_EvidenceTestBase):
    """A non-rendering upload aborts the post — "posted" never means "broken media"."""

    def _run_expecting_exit(self, host: MagicMock, **kwargs: object) -> None:
        self._patch_host(host)
        host.upload_file.return_value = {"full_path": "/-/project/9/uploads/deadbeef/x.png"}
        self._ticket()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            pytest.raises(SystemExit),
        ):
            call_command("e2e", "post-evidence", **kwargs)
        host.post_issue_comment.assert_not_called()
        host.update_issue_comment.assert_not_called()

    def test_refuses_to_post_when_upload_does_not_resolve(self) -> None:
        host = MagicMock()
        host.list_issue_comments.return_value = []
        host.verify_upload.return_value = UploadVerification(
            ok=False,
            embed_url="/uploads/deadbeef/x.png",
            detail="upload fetch returned HTTP 404",
        )
        self._run_expecting_exit(host, ticket=_ISSUE_URL, manifest=self._local_manifest())

    def test_missing_artifact_file_exits_before_any_upload(self) -> None:
        host = MagicMock()
        host.list_issue_comments.return_value = []
        manifest = json.dumps(
            {
                "ticket": "8521",
                "local": {"commits": {}},
                "workflows": [{"workflow": "Login", "local": {"images": [str(self._tmp / "absent.png")]}}],
            },
        )
        self._run_expecting_exit(host, ticket=_ISSUE_URL, manifest=manifest)
        host.upload_file.assert_not_called()


class TestRequiresManifest(_EvidenceTestBase):
    """An empty --manifest exits non-zero with no host side effect."""

    def test_missing_manifest_exits(self) -> None:
        self._ticket()
        host = MagicMock()
        self._patch_host(host)
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            pytest.raises(SystemExit),
        ):
            call_command("e2e", "post-evidence", ticket=_ISSUE_URL, manifest="")
        host.upload_file.assert_not_called()
        host.post_issue_comment.assert_not_called()


class TestOnBehalfGateConsulted(TestCase):
    """The on-behalf gate stays in front of the post — no bypass."""

    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text('[teatree]\non_behalf_post_mode = "ask"\n', encoding="utf-8")
        monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)

    def _post(self, *, comments: list[dict[str, object]]) -> MagicMock:
        host = MagicMock()
        host.list_issue_comments.return_value = comments
        post = _evidence.EvidencePost(
            issue_url=_ISSUE_URL,
            ticket_id="8521",
            title="t",
            manifest=_evidence.EvidenceManifest(
                ticket="8521",
                mrs=(),
                dev=_evidence.SideManifest(present=False),
                local=_evidence.SideManifest(present=True, commits={"client": "aabb"}),
            ),
        )
        with pytest.raises(_evidence.OnBehalfPostBlockedError):
            _evidence.post_evidence_comment(host, post)
        host.upload_file.assert_not_called()
        host.post_issue_comment.assert_not_called()
        host.update_issue_comment.assert_not_called()
        return host

    def test_create_branch_blocked_without_approval(self) -> None:
        self._post(comments=[])

    def test_update_branch_blocked_without_approval(self) -> None:
        self._post(comments=[{"id": 1, "body": "<!-- t3-e2e-evidence ticket=8521 -->\nx"}])


class TestPureHelpers:
    """The marker / state-blob / existing-note helpers are independently testable."""

    def test_marker_round_trip(self) -> None:
        marker = _evidence.evidence_marker(ticket_id="8521")
        assert _render.find_ticket_marker(f"prefix {marker} suffix", ticket_id="8521") is True
        assert _render.find_ticket_marker(f"{marker}", ticket_id="9999") is False

    def test_parse_state_blob_recovers_and_coerces(self) -> None:
        state = {"ticket": "8521", "title": "t", "mrs": [], "dev": {}, "local": {}}
        body = "<!-- t3-e2e-data " + json.dumps(state) + " -->\nrendered"
        recovered = _evidence.parse_state_blob(body)
        assert recovered["ticket"] == "8521"
        assert recovered["title"] == "t"
        # A coerced side always carries the typed keys.
        assert recovered["dev"]["workflows"] == {}
        assert recovered["local"]["commits"] == {}
        # No blob / corrupt blob → an empty (but typed) state, never a crash.
        assert _evidence.parse_state_blob("no blob here")["ticket"] == ""
        assert _evidence.parse_state_blob("<!-- t3-e2e-data {not json} -->")["ticket"] == ""

    def test_find_existing_note_keys_on_ticket_marker(self) -> None:
        comments = [
            {"id": 1, "body": "no marker"},
            {"id": 2, "body": "<!-- t3-e2e-evidence ticket=9999 -->\nother ticket"},
            {"id": 3, "body": '<!-- t3-e2e-evidence ticket=8521 -->\n<!-- t3-e2e-data {"ticket":"8521"} -->'},
        ]
        found = _evidence.find_existing_note(comments, ticket_id="8521")
        assert found is not None
        assert found.comment_id == 3
        assert found.state["ticket"] == "8521"
        assert _evidence.find_existing_note([], ticket_id="8521") is None
