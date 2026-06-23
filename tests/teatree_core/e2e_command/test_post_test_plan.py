"""Tests for ``t3 <overlay> e2e post-test-plan`` (teatree #272, #2165).

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
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core import test_plan_validation as _validation
from teatree.core.backend_protocols import UploadVerification
from teatree.core.management.commands import _test_plan
from teatree.core.management.commands import _test_plan_render as _render
from teatree.core.overlay import OverlayMetadata
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


def _write_png(path: Path, payload: bytes) -> str:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + payload)
    return str(path)


def _write_webm(path: Path, payload: bytes) -> str:
    path.write_bytes(b"\x1a\x45\xdf\xa3" + payload)
    return str(path)


def _red_boxed_png(path: Path, *, size: tuple[int, int] = (400, 300)) -> Path:
    """Write a real PNG carrying a highlightAndShoot red outline box.

    Used where the command path runs the image validator (which refuses a
    no-red-box screenshot) — the fake magic-byte ``_write_png`` is reserved for
    pure-parse tests that never reach the validator.
    """
    from PIL import Image, ImageDraw  # noqa: PLC0415

    img = Image.new("RGB", size, (245, 245, 245))
    draw = ImageDraw.Draw(img)
    w, h = size
    for off in range(6):
        draw.rectangle([20 + off, 20 + off, w - 40 - off, h - 50 - off], outline=(220, 20, 20))
    img.save(path, "PNG")
    return path


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
    ) -> _render.TestPlanState:
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
        body = _test_plan.render_body(state)
        assert "<!-- t3-e2e-evidence ticket=8521 -->" in body
        assert "<!-- t3-e2e-data " in body
        assert "## Test Plan — My feature" in body
        # Multi-repo MR links, terse repo!num labels.
        assert "Repos & MRs: [client!6331](" in body
        assert "[product!7585](" in body
        # Per-repo commit provenance for the tested side — each SHA a clickable
        # commit link derived from the matching MR URL.
        assert (
            "Local tested: [client `aaaa`](https://gitlab.com/org/client/-/commit/aaaa), "
            "[product `bbbb`](https://gitlab.com/org/product/-/commit/bbbb)" in body
        )

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
        body = _test_plan.render_body(state)
        assert "### Login" in body
        assert "| Dev | Local |" in body
        # Video row first: dev video left, local video right.
        assert "| ![v](/uploads/s/dev.webm) | ![v](/uploads/s/loc.webm) |" in body
        # Screenshot pair row.
        assert "| ![i](/uploads/s/d1.png) | ![i](/uploads/s/l1.png) |" in body
        assert "Dev deployed: [client `ddee`](https://gitlab.com/org/client/-/commit/ddee)" in body
        # Dev (ddee) and local (aabb) differ → the ± reconciliation says so.
        assert "Dev ± Local: client: ≠ dev `ddee` vs local `aabb`" in body

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
        body = _test_plan.render_body(state)
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
        body = _test_plan.render_body(state)
        assert "⚠️ Not yet on dev: client!6331 (unmerged), product!7585 (draft) — expected gap." in body

    def test_empty_video_row_is_omitted_when_neither_side_has_a_video(self) -> None:
        # Screenshots only on local, no video on either side (#272 standard): the
        # all-em-dash video row carries no information, so it is omitted entirely
        # rather than rendered as `| — | — |`.
        state = self._state(
            local={"commits": {}, "workflows": {"Search": self._embedded(images=("![i](/uploads/s/x.png)",))}},
        )
        body = _test_plan.render_body(state)
        assert "| — | — |" not in body  # the empty video row is dropped, not rendered blank
        # The screenshot pair row still renders (dev absent → em-dash left, local image right).
        assert "| — | ![i](/uploads/s/x.png) |" in body
        # The comparison table itself still renders (heading + header + the image row).
        assert "### Search" in body
        assert "| Dev | Local |" in body

    def test_video_row_renders_when_at_least_one_side_has_a_video(self) -> None:
        # Local has a video, dev does not → the video row is kept (it carries the
        # local clip), with the missing dev side as an em-dash.
        state = self._state(
            local={
                "commits": {},
                "workflows": {"Login": self._embedded(video="![v](/uploads/s/loc.webm)")},
            },
        )
        body = _test_plan.render_body(state)
        assert "| — | ![v](/uploads/s/loc.webm) |" in body

    def test_mrs_line_omitted_when_no_mrs(self) -> None:
        state = self._state(mrs=[], local={"commits": {}, "workflows": {"Wf": self._embedded(images=("![i](u)",))}})
        body = _test_plan.render_body(state)
        assert "Repos & MRs:" not in body

    def test_test_plan_steps_render_numbered_above_the_table(self) -> None:
        state = self._state(
            local={"commits": {}, "workflows": {"Login": self._embedded(images=("![i](/uploads/s/l1.png)",))}},
            steps={"Login": ["Open the app", "Click the Login button", "Expect the dashboard"]},
        )
        body = _test_plan.render_body(state)
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
        body = _test_plan.render_body(state)
        assert "**How to test:**" not in body

    def test_commit_shas_render_as_clickable_links_derived_from_mrs(self) -> None:
        # The repo short-name (client) matches the MR URL .../org/client/...,
        # so its SHA links to that project's commit page.
        state = self._state(
            local={"commits": {"client": "aabbcc"}, "workflows": {"Login": self._embedded()}},
        )
        body = _test_plan.render_body(state)
        assert "Local tested: [client `aabbcc`](https://gitlab.com/org/client/-/commit/aabbcc)" in body

    def test_commit_sha_without_matching_mr_falls_back_to_bare_codespan(self) -> None:
        # 'backend' has no MR URL → no link, bare code-span (never a broken link).
        state = self._state(
            mrs=["https://gitlab.com/org/client/-/merge_requests/6331"],
            local={"commits": {"backend": "ddeeff"}, "workflows": {"Login": self._embedded()}},
        )
        body = _test_plan.render_body(state)
        assert "Local tested: backend `ddeeff`" in body
        assert "](https://gitlab.com/org/backend/-/commit/" not in body

    def test_github_commit_link_uses_commit_path_not_dash_commit(self) -> None:
        state = self._state(
            mrs=["https://github.com/owner/product/pull/7585"],
            local={"commits": {"product": "c0ffee"}, "workflows": {"Login": self._embedded()}},
        )
        body = _test_plan.render_body(state)
        assert "[product `c0ffee`](https://github.com/owner/product/commit/c0ffee)" in body

    def test_reconcile_line_shows_same_when_dev_and_local_match(self) -> None:
        state = self._state(
            dev={"commits": {"client": "aabb"}, "missing_on_dev": [], "workflows": {"Login": self._embedded()}},
            local={"commits": {"client": "aabb"}, "workflows": {"Login": self._embedded()}},
        )
        body = _test_plan.render_body(state)
        assert "Dev ± Local: client: = same commit" in body

    def test_reconcile_line_shows_differ_with_both_shas(self) -> None:
        state = self._state(
            dev={"commits": {"client": "ddee"}, "missing_on_dev": [], "workflows": {"Login": self._embedded()}},
            local={"commits": {"client": "aabb"}, "workflows": {"Login": self._embedded()}},
        )
        body = _test_plan.render_body(state)
        assert "Dev ± Local: client: ≠ dev `ddee` vs local `aabb`" in body

    def test_reconcile_line_omitted_when_no_repo_on_both_sides(self) -> None:
        # Local only → no shared repo → no reconciliation line.
        state = self._state(
            local={"commits": {"client": "aabb"}, "workflows": {"Login": self._embedded()}},
        )
        body = _test_plan.render_body(state)
        assert "Dev ± Local:" not in body


class TestMergeState:
    """The merge over prior state freezes the side this run does not carry."""

    def _local_manifest(self) -> _test_plan.TestPlanManifest:
        return _test_plan.TestPlanManifest(
            ticket="8521",
            mrs=("https://gitlab.com/org/client/-/merge_requests/6331",),
            dev=_test_plan.SideManifest(present=False),
            local=_test_plan.SideManifest(present=True, commits={"client": "aabb"}),
        )

    def _dev_manifest(self) -> _test_plan.TestPlanManifest:
        return _test_plan.TestPlanManifest(
            ticket="8521",
            mrs=(),
            dev=_test_plan.SideManifest(present=True, commits={"client": "ddee"}, missing_on_dev=()),
            local=_test_plan.SideManifest(present=False),
        )

    def test_dev_only_run_preserves_existing_local_column(self) -> None:
        prior: _render.TestPlanState = {
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
        merged = _test_plan.merge_state(
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
        prior: _render.TestPlanState = {
            "ticket": "8521",
            "title": "t",
            "mrs": [],
            "dev": {"commits": {}, "missing_on_dev": [], "workflows": {}},
            "local": {"commits": {"client": "aabb"}, "workflows": {}},
            "steps": {"Login": ["Open the app", "Click Login"]},
        }
        merged = _test_plan.merge_state(
            prior,
            manifest=self._dev_manifest(),  # carries no steps
            title="t",
            embeds={"dev": {}, "local": {}},
        )
        assert merged["steps"]["Login"] == ["Open the app", "Click Login"]

    def test_steps_in_this_run_overwrite_prior_steps_for_that_workflow(self) -> None:
        prior = _render.empty_state(ticket="8521", title="t")
        prior["steps"] = {"Login": ["old step"]}
        manifest = _test_plan.TestPlanManifest(
            ticket="8521",
            mrs=(),
            dev=_test_plan.SideManifest(present=False),
            local=_test_plan.SideManifest(present=True, commits={"client": "aabb"}),
            steps={"Login": ("new step 1", "new step 2")},
        )
        merged = _test_plan.merge_state(prior, manifest=manifest, title="t", embeds={"dev": {}, "local": {}})
        assert merged["steps"]["Login"] == ["new step 1", "new step 2"]

    def test_local_only_run_over_empty_prior_leaves_dev_empty(self) -> None:
        merged = _test_plan.merge_state(
            _render.empty_state(ticket="8521", title="t"),
            manifest=self._local_manifest(),
            title="t",
            embeds={"dev": {}, "local": {"Login": {"video_md": "", "image_md": ["![i](/uploads/s/x.png)"]}}},
        )
        assert merged["local"]["commits"] == {"client": "aabb"}
        assert merged["dev"]["workflows"] == {}

    def test_add_dev_section_preserves_then_renders_both(self) -> None:
        # local first → render → recover state → dev run merges → both columns render.
        local_state = _test_plan.merge_state(
            _render.empty_state(ticket="8521", title="My feature"),
            manifest=self._local_manifest(),
            title="My feature",
            embeds={
                "dev": {},
                "local": {"Login": {"video_md": "![v](/uploads/s/l.webm)", "image_md": ["![i](/uploads/s/l1.png)"]}},
            },
        )
        local_state["ticket"] = "8521"
        body_after_local = _test_plan.render_body(local_state)
        recovered = _test_plan.parse_state_blob(body_after_local)

        dev_state = _test_plan.merge_state(
            recovered,
            manifest=self._dev_manifest(),
            title="My feature",
            embeds={
                "dev": {"Login": {"video_md": "![v](/uploads/s/d.webm)", "image_md": ["![i](/uploads/s/d1.png)"]}},
                "local": {},
            },
        )
        dev_state["ticket"] = "8521"
        final = _test_plan.render_body(dev_state)
        # Both columns are present and paired.
        assert "| ![v](/uploads/s/d.webm) | ![v](/uploads/s/l.webm) |" in final
        assert "| ![i](/uploads/s/d1.png) | ![i](/uploads/s/l1.png) |" in final
        # Local survived the dev-only merge untouched (rendered as a commit link).
        assert "Local tested: [client `aabb`](https://gitlab.com/org/client/-/commit/aabb)" in final


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
        parsed = _test_plan.parse_manifest(manifest)
        assert parsed.ticket == "8521"
        assert parsed.local.present is True
        assert parsed.dev.present is False
        wf = parsed.local.workflows["Login"]
        assert wf.video is not None
        assert len(wf.images) == 1

    def test_rejects_invalid_json(self) -> None:
        with pytest.raises(_test_plan.TestPlanValidationError, match="not valid JSON"):
            _test_plan.parse_manifest("{not json")

    def test_rejects_missing_workflows(self) -> None:
        with pytest.raises(_test_plan.TestPlanValidationError, match="workflows"):
            _test_plan.parse_manifest(json.dumps({"ticket": "8521", "local": {}}))

    def test_rejects_missing_artifact_file(self, tmp_path: Path) -> None:
        manifest = self._manifest(tmp_path, video=None, images=[str(tmp_path / "absent.png")])
        with pytest.raises(_test_plan.TestPlanValidationError, match="not found"):
            _test_plan.parse_manifest(manifest)

    def test_rejects_wrong_media_kind_for_video_slot(self, tmp_path: Path) -> None:
        # A .png handed to the video slot must be rejected.
        png = _write_png(tmp_path / "still.png", b"X")
        manifest = self._manifest(tmp_path, video=png, images=[_write_png(tmp_path / "ok.png", b"Y")])
        with pytest.raises(_test_plan.TestPlanValidationError, match="not a recognised video"):
            _test_plan.parse_manifest(manifest)

    def test_rejects_when_no_side_carries_captures(self, tmp_path: Path) -> None:
        manifest = json.dumps(
            {"ticket": "8521", "workflows": [{"workflow": "Login"}]},
        )
        with pytest.raises(_test_plan.TestPlanValidationError, match="no 'dev' or 'local'"):
            _test_plan.parse_manifest(manifest)

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
        parsed = _test_plan.parse_manifest(manifest)
        assert parsed.steps["Login"] == ("Open the app", "Click Login", "Expect the dashboard")
        assert "Search" not in parsed.steps


class TestRefuseStillsOnly:
    """The stills-only validator: screenshots present + no video anywhere → refuse."""

    def test_stills_only_refused(self) -> None:
        with pytest.raises(_validation.TestPlanImageValidationError, match="no video"):
            _validation.refuse_stills_only(has_image=True, has_video=False, allow_no_video=False)

    def test_stills_only_passes_with_allow_no_video(self) -> None:
        _validation.refuse_stills_only(has_image=True, has_video=False, allow_no_video=True)

    def test_with_video_passes(self) -> None:
        _validation.refuse_stills_only(has_image=True, has_video=True, allow_no_video=False)

    def test_no_image_is_not_stills_only(self) -> None:
        # A steps-only / no-media manifest never trips this gate (#2269 owns it).
        _validation.refuse_stills_only(has_image=False, has_video=False, allow_no_video=False)


class TestNoVideoGateAtCommand(TestCase):
    """``build_validated_post`` refuses a stills-only manifest unless ``--allow-no-video``."""

    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._tmp = tmp_path
        monkeypatch.setattr(
            _test_plan,
            "resolve_worktree",
            MagicMock(side_effect=_test_plan.WorktreeNotFoundError("none")),
        )

    def _ticket(self) -> None:
        from teatree.core.models import Ticket  # noqa: PLC0415

        Ticket.objects.create(overlay="test", issue_url=_ISSUE_URL)

    def _manifest(self, *, video: bool) -> str:
        local: dict[str, object] = {"images": [str(_red_boxed_png(self._tmp / "a.png"))]}
        if video:
            local["video"] = _write_webm(self._tmp / "v.webm", b"V")
        return json.dumps(
            {
                "ticket": "8521",
                "local": {"commits": {"client": "aabb"}},
                "workflows": [{"workflow": "Login", "local": local}],
            },
        )

    def test_stills_only_manifest_is_refused(self) -> None:
        self._ticket()
        flags = _test_plan.TestPlanFlags(ticket="", manifest=self._manifest(video=False))
        with pytest.raises(_test_plan.TestPlanValidationError, match="no video"):
            _test_plan.build_validated_post(flags)

    def test_stills_only_manifest_passes_with_allow_no_video(self) -> None:
        self._ticket()
        flags = _test_plan.TestPlanFlags(ticket="", manifest=self._manifest(video=False), allow_no_video=True)
        post = _test_plan.build_validated_post(flags)
        assert post.issue_url == _ISSUE_URL

    def test_manifest_with_a_video_passes(self) -> None:
        self._ticket()
        flags = _test_plan.TestPlanFlags(ticket="", manifest=self._manifest(video=True))
        post = _test_plan.build_validated_post(flags)
        assert post.issue_url == _ISSUE_URL


class TestManifestPathResolution:
    """Relative image/video paths resolve against the manifest file's directory (#friction)."""

    def test_relative_paths_resolve_against_base_dir(self, tmp_path: Path) -> None:
        media_dir = tmp_path / "artifacts"
        media_dir.mkdir()
        _write_png(media_dir / "shot.png", b"A")
        _write_webm(media_dir / "run.webm", b"V")
        # The manifest carries BARE relative names; base_dir is the manifest's dir.
        manifest = json.dumps(
            {
                "ticket": "8521",
                "local": {"commits": {"client": "aabb"}},
                "workflows": [{"workflow": "Login", "local": {"video": "run.webm", "images": ["shot.png"]}}],
            },
        )
        parsed = _test_plan.parse_manifest(manifest, base_dir=media_dir)
        wf = parsed.local.workflows["Login"]
        assert wf.images[0] == media_dir / "shot.png"
        assert wf.video == media_dir / "run.webm"

    def test_absolute_paths_pass_through_unchanged(self, tmp_path: Path) -> None:
        abs_img = _write_png(tmp_path / "abs.png", b"A")
        manifest = json.dumps(
            {
                "ticket": "8521",
                "local": {"commits": {"client": "aabb"}},
                "workflows": [{"workflow": "Login", "local": {"images": [abs_img]}}],
            },
        )
        # A different (wrong) base_dir must NOT affect an absolute path.
        parsed = _test_plan.parse_manifest(manifest, base_dir=tmp_path / "elsewhere")
        assert parsed.local.workflows["Login"].images[0] == Path(abs_img)

    def test_relative_path_without_base_dir_still_resolves_from_cwd(self, tmp_path: Path) -> None:
        """Back-compat: no base_dir keeps the legacy cwd-relative behaviour."""
        _write_png(tmp_path / "shot.png", b"A")
        manifest = json.dumps(
            {
                "ticket": "8521",
                "local": {"commits": {"client": "aabb"}},
                "workflows": [{"workflow": "Login", "local": {"images": ["shot.png"]}}],
            },
        )
        with pytest.raises(_test_plan.TestPlanValidationError, match="not found"):
            # No base_dir and cwd is not tmp_path → the bare name does not resolve.
            _test_plan.parse_manifest(manifest)


class TestTicketFallbackFromManifest(TestCase):
    """``--ticket`` omitted falls back to the manifest's top-level ``ticket`` field (#friction)."""

    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        self._tmp = tmp_path
        # No worktree → the resolution must come from the manifest's ticket field.
        monkeypatch.setattr(
            _test_plan,
            "resolve_worktree",
            MagicMock(side_effect=_test_plan.WorktreeNotFoundError("none")),
        )

    def test_manifest_ticket_field_used_when_flag_omitted(self) -> None:
        from teatree.core.models import Ticket  # noqa: PLC0415

        Ticket.objects.create(overlay="test", issue_url=_ISSUE_URL)
        img = _red_boxed_png(self._tmp / "a.png")
        manifest = json.dumps(
            {
                "ticket": "8521",
                "local": {"commits": {"client": "aabb"}},
                "workflows": [{"workflow": "Login", "local": {"images": [str(img)]}}],
            },
        )
        flags = _test_plan.TestPlanFlags(ticket="", manifest=manifest, allow_no_video=True)
        post = _test_plan.build_validated_post(flags)
        assert post.issue_url == _ISSUE_URL

    def test_missing_ticket_everywhere_raises_resolution_error(self) -> None:
        img = _red_boxed_png(self._tmp / "a.png")
        manifest = json.dumps(
            {
                "local": {"commits": {"client": "aabb"}},
                "workflows": [{"workflow": "Login", "local": {"images": [str(img)]}}],
            },
        )
        flags = _test_plan.TestPlanFlags(ticket="", manifest=manifest, allow_no_video=True)
        with pytest.raises(_test_plan.TestPlanResolutionError, match="Could not determine the ticket"):
            _test_plan.build_validated_post(flags)


class TestMrLabel:
    """The MR link rendering is a pure helper."""

    def test_gitlab_mr_renders_repo_bang_num(self) -> None:
        line = _test_plan.render_mrs_line(("https://gitlab.com/grp/sub/client/-/merge_requests/6331",))
        assert line == "Repos & MRs: [client!6331](https://gitlab.com/grp/sub/client/-/merge_requests/6331)"

    def test_github_pr_renders_repo_hash_num(self) -> None:
        line = _test_plan.render_mrs_line(("https://github.com/owner/product/pull/7585",))
        assert line == "Repos & MRs: [product#7585](https://github.com/owner/product/pull/7585)"

    def test_non_url_ref_shown_verbatim(self) -> None:
        line = _test_plan.render_mrs_line(("client!6331",))
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
        self._monkeypatch.setattr(_test_plan, "code_host_from_overlay", lambda: host)
        self._monkeypatch.setattr(
            _test_plan,
            "resolve_worktree",
            MagicMock(side_effect=_test_plan.WorktreeNotFoundError("none")),
        )

    def _ticket(self) -> None:
        from teatree.core.models import Ticket  # noqa: PLC0415

        Ticket.objects.create(overlay="test", issue_url=_ISSUE_URL)

    def _local_manifest(self) -> str:
        img = str(_red_boxed_png(self._tmp / "step1.png"))
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
            return cast("dict[str, object]", call_command("e2e", "post-test-plan", **kwargs))

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
        img = str(_red_boxed_png(self._tmp / "step1.png"))
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
            call_command("e2e", "post-test-plan", ticket=_ISSUE_URL, manifest=self._multi_repo_manifest())

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
            call_command("e2e", "post-test-plan", **kwargs)
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


class TestImagePreflightAtCommand(_EvidenceTestBase):
    """The red-box preflight refuses a no-red-box screenshot at the command, before upload."""

    def _no_red_box_manifest(self) -> str:
        # A real (Pillow-openable) PNG with NO red highlight box.
        from PIL import Image  # noqa: PLC0415

        plain = self._tmp / "plain.png"
        Image.new("RGB", (400, 300), (240, 240, 240)).save(plain, "PNG")
        return json.dumps(
            {
                "ticket": "8521",
                "local": {"commits": {"client": "aabb"}},
                "workflows": [{"workflow": "Login", "local": {"images": [str(plain)]}}],
            },
        )

    def test_no_red_box_refused_before_upload(self) -> None:
        self._ticket()
        host = MagicMock()
        host.list_issue_comments.return_value = []
        self._patch_host(host)
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            pytest.raises(SystemExit),
        ):
            call_command("e2e", "post-test-plan", ticket=_ISSUE_URL, manifest=self._no_red_box_manifest())
        host.upload_file.assert_not_called()
        host.post_issue_comment.assert_not_called()

    def test_skip_validation_lets_a_no_red_box_post_through(self) -> None:
        self._ticket()
        host = MagicMock()
        host.list_issue_comments.return_value = []
        host.post_issue_comment.return_value = {"id": 88, "web_url": "u"}
        host.upload_file.return_value = {"full_path": "/-/project/9/uploads/deadbeef/x.png"}
        host.verify_upload.return_value = UploadVerification(ok=True, embed_url="/uploads/deadbeef/x.png")
        host.repo_for_issue_url.return_value = "org/repo"
        self._patch_host(host)
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast(
                "dict[str, object]",
                call_command(
                    "e2e",
                    "post-test-plan",
                    ticket=_ISSUE_URL,
                    manifest=self._no_red_box_manifest(),
                    skip_validation=True,
                    allow_no_video=True,
                ),
            )
        assert result["action"] == "created"
        host.post_issue_comment.assert_called_once()


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
            call_command("e2e", "post-test-plan", ticket=_ISSUE_URL, manifest="")
        host.upload_file.assert_not_called()
        host.post_issue_comment.assert_not_called()


class TestOnBehalfGateConsulted(TestCase):
    """The on-behalf gate stays in front of the post when evidence is NOT auto-allowed.

    ``post_e2e_evidence`` is in the default ``on_behalf_auto_actions`` allowlist
    (the user does not approve their own evidence posts), so this suite clears
    that allowlist to prove the gate still blocks when a user opts back into
    gating. The default carve-out (gate auto-proceeds) is covered by
    :class:`TestOnBehalfEvidenceAutoProceeds`.
    """

    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # ``on_behalf_post_mode`` and ``on_behalf_auto_actions`` are DB-home
        # (#1775): a ``[teatree]`` TOML value is ignored on read. Stage both via
        # their ``T3_*`` env tier (the highest, DB-home-compatible layer): ASK
        # mode plus an empty auto-actions allowlist so the gate actually blocks.
        self._monkeypatch = monkeypatch
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text("[teatree]\n", encoding="utf-8")
        monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
        monkeypatch.setenv("T3_ON_BEHALF_POST_MODE", "ask")
        monkeypatch.setenv("T3_ON_BEHALF_AUTO_ACTIONS", "")

    def _post(self, *, comments: list[dict[str, object]]) -> MagicMock:
        host = MagicMock()
        host.list_issue_comments.return_value = comments
        post = _test_plan.TestPlanPost(
            issue_url=_ISSUE_URL,
            ticket_id="8521",
            title="t",
            manifest=_test_plan.TestPlanManifest(
                ticket="8521",
                mrs=(),
                dev=_test_plan.SideManifest(present=False),
                local=_test_plan.SideManifest(present=True, commits={"client": "aabb"}),
            ),
        )
        with pytest.raises(_test_plan.OnBehalfPostBlockedError):
            _test_plan.post_test_plan_comment(host, post)
        host.upload_file.assert_not_called()
        host.post_issue_comment.assert_not_called()
        host.update_issue_comment.assert_not_called()
        return host

    def test_create_branch_blocked_without_approval(self) -> None:
        self._post(comments=[])

    def test_update_branch_blocked_without_approval(self) -> None:
        self._post(comments=[{"id": 1, "body": "<!-- t3-e2e-evidence ticket=8521 -->\nx"}])


class TestOnBehalfEvidenceAutoProceeds(TestCase):
    """Under the DEFAULT allowlist, ``post_e2e_evidence`` proceeds even under ASK.

    The user does not approve their own evidence posts — ``post_e2e_evidence``
    is in the default ``on_behalf_auto_actions`` carve-out — so a blocking mode
    does NOT raise the on-behalf block for the evidence path.
    """

    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # ``on_behalf_post_mode`` is DB-home (#1775): a TOML value is ignored on
        # read, so ASK mode is staged via its ``T3_*`` env tier. The default
        # ``on_behalf_auto_actions`` carve-out is left intact (evidence proceeds).
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text("[teatree]\n", encoding="utf-8")
        monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
        monkeypatch.setenv("T3_ON_BEHALF_POST_MODE", "ask")

    def test_post_proceeds_without_approval_under_ask(self) -> None:
        host = MagicMock()
        host.list_issue_comments.return_value = []
        host.post_issue_comment.return_value = {"id": 91, "web_url": "u"}
        host.upload_file.return_value = {"full_path": "/-/project/9/uploads/deadbeef/x.png"}
        host.verify_upload.return_value = UploadVerification(ok=True, embed_url="/uploads/deadbeef/x.png")
        host.repo_for_issue_url.return_value = "org/repo"
        post = _test_plan.TestPlanPost(
            issue_url=_ISSUE_URL,
            ticket_id="8521",
            title="t",
            manifest=_test_plan.TestPlanManifest(
                ticket="8521",
                mrs=(),
                dev=_test_plan.SideManifest(present=False),
                local=_test_plan.SideManifest(present=True, commits={"client": "aabb"}),
            ),
        )
        # No OnBehalfPostBlockedError despite ASK mode — the carve-out proceeds.
        result = _test_plan.post_test_plan_comment(host, post)
        assert result["action"] == "created"
        host.post_issue_comment.assert_called_once()


class TestPureHelpers:
    """The marker / state-blob / existing-note helpers are independently testable."""

    def test_marker_round_trip(self) -> None:
        marker = _test_plan.test_plan_marker(ticket_id="8521")
        assert _render.find_ticket_marker(f"prefix {marker} suffix", ticket_id="8521") is True
        assert _render.find_ticket_marker(f"{marker}", ticket_id="9999") is False

    def test_parse_state_blob_recovers_and_coerces(self) -> None:
        state = {"ticket": "8521", "title": "t", "mrs": [], "dev": {}, "local": {}}
        body = "<!-- t3-e2e-data " + json.dumps(state) + " -->\nrendered"
        recovered = _test_plan.parse_state_blob(body)
        assert recovered["ticket"] == "8521"
        assert recovered["title"] == "t"
        # A coerced side always carries the typed keys.
        assert recovered["dev"]["workflows"] == {}
        assert recovered["local"]["commits"] == {}
        # No blob / corrupt blob → an empty (but typed) state, never a crash.
        assert _test_plan.parse_state_blob("no blob here")["ticket"] == ""
        assert _test_plan.parse_state_blob("<!-- t3-e2e-data {not json} -->")["ticket"] == ""

    def test_find_existing_note_keys_on_ticket_marker(self) -> None:
        comments = [
            {"id": 1, "body": "no marker"},
            {"id": 2, "body": "<!-- t3-e2e-evidence ticket=9999 -->\nother ticket"},
            {"id": 3, "body": '<!-- t3-e2e-evidence ticket=8521 -->\n<!-- t3-e2e-data {"ticket":"8521"} -->'},
        ]
        found = _test_plan.find_existing_note(comments, ticket_id="8521")
        assert found is not None
        assert found.comment_id == 3
        assert found.state["ticket"] == "8521"
        assert _test_plan.find_existing_note([], ticket_id="8521") is None


# --- zero-media rejection ---------------------------------------------------


class TestZeroMediaRejection:
    """A manifest where every workflow on every present side has no media is rejected."""

    def test_rejects_when_present_side_has_no_media_anywhere(self, tmp_path: Path) -> None:
        manifest = json.dumps(
            {
                "ticket": "8521",
                "local": {"commits": {"client": "aabb"}},
                "workflows": [
                    {"workflow": "Login", "local": {"images": [], "video": None}},
                    {"workflow": "Search", "local": {"images": []}},
                ],
            },
        )
        with pytest.raises(_test_plan.TestPlanValidationError, match="no media"):
            _test_plan.parse_manifest(manifest)

    def test_rejects_manifest_with_commits_but_zero_workflow_captures(self, tmp_path: Path) -> None:
        manifest = json.dumps(
            {
                "ticket": "8521",
                "local": {"commits": {"client": "aabb"}},
                "workflows": [{"workflow": "Login", "local": {}}],
            },
        )
        with pytest.raises(_test_plan.TestPlanValidationError, match="no media"):
            _test_plan.parse_manifest(manifest)

    def test_accepts_manifest_with_at_least_one_image(self, tmp_path: Path) -> None:
        img = _write_png(tmp_path / "a.png", b"A")
        manifest = json.dumps(
            {
                "ticket": "8521",
                "local": {"commits": {"client": "aabb"}},
                "workflows": [{"workflow": "Login", "local": {"images": [img]}}],
            },
        )
        parsed = _test_plan.parse_manifest(manifest)
        assert parsed.local.present is True

    def test_accepts_manifest_with_only_a_video(self, tmp_path: Path) -> None:
        vid = _write_webm(tmp_path / "run.webm", b"V")
        manifest = json.dumps(
            {
                "ticket": "8521",
                "local": {"commits": {"client": "aabb"}},
                "workflows": [{"workflow": "Login", "local": {"video": vid}}],
            },
        )
        parsed = _test_plan.parse_manifest(manifest)
        assert parsed.local.present is True

    def test_two_sides_both_zero_media_rejected(self, tmp_path: Path) -> None:
        manifest = json.dumps(
            {
                "ticket": "8521",
                "dev": {"commits": {"client": "ddee"}},
                "local": {"commits": {"client": "aabb"}},
                "workflows": [{"workflow": "Login", "dev": {}, "local": {}}],
            },
        )
        with pytest.raises(_test_plan.TestPlanValidationError, match="no media"):
            _test_plan.parse_manifest(manifest)

    def test_one_side_has_media_other_side_is_empty_accepted(self, tmp_path: Path) -> None:
        img = _write_png(tmp_path / "a.png", b"A")
        manifest = json.dumps(
            {
                "ticket": "8521",
                "dev": {"commits": {"client": "ddee"}},
                "local": {"commits": {"client": "aabb"}},
                "workflows": [{"workflow": "Login", "dev": {}, "local": {"images": [img]}}],
            },
        )
        parsed = _test_plan.parse_manifest(manifest)
        assert parsed.local.present is True


# --- retract-evidence command -----------------------------------------------


class TestRetractEvidence(_EvidenceTestBase):
    """``e2e retract-evidence`` deletes the ticket's single test-plan note."""

    def _existing_note_body(self) -> str:
        marker = "<!-- t3-e2e-evidence ticket=8521 -->"
        state: dict[str, object] = {
            "ticket": "8521",
            "title": "t",
            "mrs": [],
            "dev": {"commits": {}, "missing_on_dev": [], "workflows": {}},
            "local": {"commits": {"client": "aabb"}, "workflows": {}},
            "steps": {},
        }
        blob = "<!-- t3-e2e-data " + json.dumps(state, separators=(",", ":"), sort_keys=True) + " -->"
        return f"{marker}\n{blob}\n## Test Plan — t\n"

    def test_deletes_existing_note(self) -> None:
        self._ticket()
        host = MagicMock()
        host.list_issue_comments.return_value = [{"id": 42, "body": self._existing_note_body()}]
        host.delete_issue_comment.return_value = {}
        self._patch_host(host)
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            call_command("e2e", "retract-evidence", ticket=_ISSUE_URL)
        host.delete_issue_comment.assert_called_once_with(issue_url=_ISSUE_URL, comment_id=42)

    def test_exits_nonzero_when_no_note_exists(self) -> None:
        self._ticket()
        host = MagicMock()
        host.list_issue_comments.return_value = []
        self._patch_host(host)
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            pytest.raises(SystemExit),
        ):
            call_command("e2e", "retract-evidence", ticket=_ISSUE_URL)
        host.delete_issue_comment.assert_not_called()

    def test_exits_nonzero_when_no_code_host(self) -> None:
        self._ticket()
        self._monkeypatch.setattr(_test_plan, "code_host_from_overlay", lambda: None)
        self._monkeypatch.setattr(
            _test_plan,
            "resolve_worktree",
            MagicMock(side_effect=_test_plan.WorktreeNotFoundError("none")),
        )
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            pytest.raises(SystemExit),
        ):
            call_command("e2e", "retract-evidence", ticket=_ISSUE_URL)


# --- #2304: templates, never-render-empty, --body-file ----------------------


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


class TestBrowserClickFirstStepsWithoutMedia(TestCase):
    """A steps-only manifest (steps, no screenshots/video) must still render the steps."""

    def _steps_only_state(self) -> _render.TestPlanState:
        return {
            "ticket": "8521",
            "title": "Login flow",
            "mrs": [],
            "dev": _empty_side(env="dev"),
            "local": {"commits": {"client": "aabb"}, "workflows": {}},
            "steps": {"Login": ["Open the app", "Click Login", "Expect dashboard"]},
            "template": "browser-click-first",
        }

    def test_renders_steps_when_no_media(self) -> None:
        body = _render.render_body(self._steps_only_state())
        visible = body.split("-->")[-1]
        assert "### Login" in visible
        assert "1. Open the app" in visible
        assert "2. Click Login" in visible
        assert "3. Expect dashboard" in visible

    def test_renders_steps_when_no_media_via_production_path(self) -> None:
        manifest = _render.parse_manifest(
            json.dumps(
                {
                    "ticket": "8521",
                    "template": "browser-click-first",
                    "local": {"commits": {"client": "aabb"}},
                    "workflows": [{"workflow": "Login", "steps": ["Open the app", "Click Login"]}],
                }
            )
        )
        merged = _render.merge_state(
            _render.empty_state(ticket="8521", title="t"),
            manifest=manifest,
            title="Login flow",
            embeds={"dev": {}, "local": {}},
        )
        body = _render.render_body(merged)
        visible = body.split("-->")[-1]
        assert "### Login" in visible
        assert "1. Open the app" in visible
        assert "2. Click Login" in visible

    def test_media_and_steps_both_render(self) -> None:
        state: _render.TestPlanState = {
            "ticket": "8521",
            "title": "Login flow",
            "mrs": [],
            "dev": _empty_side(env="dev"),
            "local": _local_side({"Login": {"video_md": "", "image_md": ["![s1](/uploads/s/s1.png)"]}}),
            "steps": {"Login": ["Open the app", "Click Login"]},
            "template": "browser-click-first",
        }
        body = _render.render_body(state)
        visible = body.split("-->")[-1]
        assert "1. Open the app" in visible
        assert "![s1](/uploads/s/s1.png)" in visible


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


class TestLinkApiStepsRendered(TestCase):
    """A steps-only ``link-api`` manifest (steps, no link/code embeds) must render the steps."""

    def _steps_only_state(self) -> _render.TestPlanState:
        return {
            "ticket": "8521",
            "title": "API check",
            "mrs": [],
            "dev": _empty_side(env="dev"),
            "local": {"commits": {"client": "aabb"}, "workflows": {}},
            "steps": {"Create user": ["POST /users", "Assert 201", "GET /users/1"]},
            "template": "link-api",
        }

    def test_renders_how_to_test_steps_when_no_media(self) -> None:
        body = _render.render_body(self._steps_only_state())
        visible = body.split("-->")[-1]
        assert "### Create user" in visible
        assert "**How to test:**" in visible
        assert "1. POST /users" in visible
        assert "2. Assert 201" in visible
        assert "3. GET /users/1" in visible

    def test_renders_steps_via_production_path(self) -> None:
        manifest = _render.parse_manifest(
            json.dumps(
                {
                    "ticket": "8521",
                    "template": "link-api",
                    "local": {"commits": {"client": "aabb"}},
                    "workflows": [{"workflow": "Create user", "steps": ["POST /users", "Assert 201"]}],
                }
            )
        )
        merged = _render.merge_state(
            _render.empty_state(ticket="8521", title="t"),
            manifest=manifest,
            title="API check",
            embeds={"dev": {}, "local": {}},
        )
        body = _render.render_body(merged)
        visible = body.split("-->")[-1]
        assert "### Create user" in visible
        assert "1. POST /users" in visible
        assert "2. Assert 201" in visible

    def test_renders_steps_alongside_link_and_code(self) -> None:
        state: _render.TestPlanState = {
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
            "steps": {"Create user": ["POST /users", "Assert 201"]},
            "template": "link-api",
        }
        body = _render.render_body(state)
        visible = body.split("-->")[-1]
        assert "1. POST /users" in visible
        assert "[POST /users]" in visible
        assert "```json" in visible


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
        manifest = _render.parse_manifest(
            json.dumps({"ticket": "8521", "local": {}, "workflows": [{"workflow": "X", "steps": ["s"]}]})
        )
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


class TestTemplateFlag(TestCase):
    def test_template_flag_overrides_manifest_default(self) -> None:
        flags = _test_plan.TestPlanFlags(
            ticket="8521",
            manifest=json.dumps({"ticket": "8521", "local": {}, "workflows": [{"workflow": "Login", "steps": ["s"]}]}),
            template="browser-click-first",
        )
        with patch("teatree.core.management.commands._test_plan._resolve_worktree_or_none", return_value=None):
            ticket = MagicMock()
            ticket.issue_url = _ISSUE_URL
            ticket.ticket_number = "8521"
            with patch("teatree.core.models.Ticket.objects.resolve", return_value=ticket):
                post = _test_plan.build_validated_post(flags)
        assert post.manifest.template == "browser-click-first"
