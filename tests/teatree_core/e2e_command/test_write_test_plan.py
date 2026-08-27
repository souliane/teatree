"""Tests for ``t3 <overlay> e2e write-test-plan`` (teatree #272).

The one-file-per-ticket test-plan model: ``test-plans/<repo>-<ticket>.md`` in the e2e
repo, rendering a side-by-side ``Dev | Local`` plan and accumulating environment
columns across runs via a hidden machine-readable state blob.

The pure-builder half exercises the manifest parse, the merge over prior state,
the side-by-side render (videos row, screenshot pairs, em-dash cells, the
dev-gap line, per-repo commit + run-instant provenance, MR links), and the
splice that adds a dev column while preserving a frozen local column.

The command half asserts the plan lands at the derived path, a second run
updates that one file, nothing is posted to any forge, and the capture /
blocked-body gates refuse a bad plan with nothing written.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.evidence import test_plan_validation as _validation
from teatree.core.evidence.test_plan_blocked_gate import BlockedTestPlanPostError
from teatree.core.management.commands._test_plan import mr_post as _mr_post
from teatree.core.management.commands._test_plan import render as _render
from teatree.core.management.commands._test_plan import render as _test_plan
from teatree.core.management.commands._test_plan import scenario as _scenario
from teatree.core.management.commands._test_plan import write as _write
from teatree.core.management.commands._test_plan.render import PlanState, render_body
from teatree.core.management.commands.e2e import Command as E2eCommand
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayMetadata
from tests.teatree_core.conftest import CommandOverlay

_ISSUE_URL = "https://gitlab.com/org/repo/-/issues/8521"
_E2E_REPO = "client-workspace"


class _E2eRepoMetadata(OverlayMetadata):
    def get_e2e_config(self) -> dict[str, str]:
        return {"runner": "external", "project_path": f"org/{_E2E_REPO}", "e2e_dir": "e2e"}


class _E2eRepoOverlay(CommandOverlay):
    metadata = _E2eRepoMetadata()

    def get_repos(self) -> list[str]:
        return [_E2E_REPO]


_MOCK_OVERLAY = {"test": _E2eRepoOverlay()}
_E2E_OVERLAY = _MOCK_OVERLAY
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
    ) -> PlanState:
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
        body = render_body(state)
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
        body = render_body(state)
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
        body = render_body(state)
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
        body = render_body(state)
        assert "⚠️ Not yet on dev: client!6331 (unmerged), product!7585 (draft) — expected gap." in body

    def test_empty_video_row_is_omitted_when_neither_side_has_a_video(self) -> None:
        # Screenshots only on local, no video on either side (#272 standard): the
        # all-em-dash video row carries no information, so it is omitted entirely
        # rather than rendered as `| — | — |`.
        state = self._state(
            local={"commits": {}, "workflows": {"Search": self._embedded(images=("![i](/uploads/s/x.png)",))}},
        )
        body = render_body(state)
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
        body = render_body(state)
        assert "| — | ![v](/uploads/s/loc.webm) |" in body

    def test_mrs_line_omitted_when_no_mrs(self) -> None:
        state = self._state(mrs=[], local={"commits": {}, "workflows": {"Wf": self._embedded(images=("![i](u)",))}})
        body = render_body(state)
        assert "Repos & MRs:" not in body

    def test_test_plan_steps_render_numbered_above_the_table(self) -> None:
        state = self._state(
            local={"commits": {}, "workflows": {"Login": self._embedded(images=("![i](/uploads/s/l1.png)",))}},
            steps={"Login": ["Open the app", "Click the Login button", "Expect the dashboard"]},
        )
        body = render_body(state)
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
        body = render_body(state)
        assert "**How to test:**" not in body

    def test_backend_only_workflow_suppresses_the_empty_dev_local_table(self) -> None:
        # A backend/API workflow carries neither video nor screenshots on either
        # side — only its steps, which include the `Actual: ✅` claim. Emitting
        # the `| Dev | Local |` header alone renders an empty grid that reads as
        # missing evidence; the workflow renders as heading + steps only.
        state = self._state(
            local={"commits": {}, "workflows": {}},
            steps={"Backend fee removal": ["Load the offer serializer", "Actual: ✅ fee line absent from payload"]},
        )
        body = render_body(state)
        assert "### Backend fee removal" in body
        assert "1. Load the offer serializer" in body
        assert "2. Actual: ✅ fee line absent from payload" in body
        assert "| Dev | Local |" not in body
        assert "|---|---|" not in body

    def test_empty_embed_workflow_suppresses_the_empty_dev_local_table(self) -> None:
        # A workflow present in a side's map but carrying an empty embed (no
        # video, no screenshots) is the same imageless case — suppress its table.
        state = self._state(
            local={"commits": {}, "workflows": {"Backend claim": self._embedded()}},
        )
        body = render_body(state)
        assert "### Backend claim" in body
        assert "| Dev | Local |" not in body

    def test_media_bearing_workflow_still_renders_the_table_alongside_a_backend_one(self) -> None:
        # A backend-only workflow suppresses its table; a media-bearing sibling
        # in the same plan still renders its Dev | Local comparison table.
        state = self._state(
            local={
                "commits": {},
                "workflows": {"UI login": self._embedded(images=("![i](/uploads/s/l1.png)",))},
            },
            steps={"Backend fee removal": ["Load the serializer", "Actual: ✅ absent"]},
        )
        body = render_body(state)
        assert "### UI login" in body
        assert "| Dev | Local |" in body  # the media-bearing workflow keeps its table
        assert "| — | ![i](/uploads/s/l1.png) |" in body
        assert "### Backend fee removal" in body

    def test_commit_shas_render_as_clickable_links_derived_from_mrs(self) -> None:
        # The repo short-name (client) matches the MR URL .../org/client/...,
        # so its SHA links to that project's commit page.
        state = self._state(
            local={"commits": {"client": "aabbcc"}, "workflows": {"Login": self._embedded()}},
        )
        body = render_body(state)
        assert "Local tested: [client `aabbcc`](https://gitlab.com/org/client/-/commit/aabbcc)" in body

    def test_commit_sha_without_matching_mr_falls_back_to_bare_codespan(self) -> None:
        # 'backend' has no MR URL → no link, bare code-span (never a broken link).
        state = self._state(
            mrs=["https://gitlab.com/org/client/-/merge_requests/6331"],
            local={"commits": {"backend": "ddeeff"}, "workflows": {"Login": self._embedded()}},
        )
        body = render_body(state)
        assert "Local tested: backend `ddeeff`" in body
        assert "](https://gitlab.com/org/backend/-/commit/" not in body

    def test_github_commit_link_uses_commit_path_not_dash_commit(self) -> None:
        state = self._state(
            mrs=["https://github.com/owner/product/pull/7585"],
            local={"commits": {"product": "c0ffee"}, "workflows": {"Login": self._embedded()}},
        )
        body = render_body(state)
        assert "[product `c0ffee`](https://github.com/owner/product/commit/c0ffee)" in body

    def test_reconcile_line_shows_same_when_dev_and_local_match(self) -> None:
        state = self._state(
            dev={"commits": {"client": "aabb"}, "missing_on_dev": [], "workflows": {"Login": self._embedded()}},
            local={"commits": {"client": "aabb"}, "workflows": {"Login": self._embedded()}},
        )
        body = render_body(state)
        assert "Dev ± Local: client: = same commit" in body

    def test_reconcile_line_shows_differ_with_both_shas(self) -> None:
        state = self._state(
            dev={"commits": {"client": "ddee"}, "missing_on_dev": [], "workflows": {"Login": self._embedded()}},
            local={"commits": {"client": "aabb"}, "workflows": {"Login": self._embedded()}},
        )
        body = render_body(state)
        assert "Dev ± Local: client: ≠ dev `ddee` vs local `aabb`" in body

    def test_reconcile_line_omitted_when_no_repo_on_both_sides(self) -> None:
        # Local only → no shared repo → no reconciliation line.
        state = self._state(
            local={"commits": {"client": "aabb"}, "workflows": {"Login": self._embedded()}},
        )
        body = render_body(state)
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
        prior: PlanState = {
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
        prior: PlanState = {
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
        body_after_local = render_body(local_state)
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
        final = render_body(dev_state)
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


def _real_video(path: Path) -> str:
    """A short ffmpeg-rendered clip — the video gate reads it with ffprobe, so it must be real."""
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            *("-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=4"),
            *("-pix_fmt", "yuv420p", str(path)),
        ],
        check=True,
        capture_output=True,
    )
    return str(path)


def _seed_ticket_with_e2e_worktree(tmp: Path) -> Ticket:
    """A ticket whose e2e-repo worktree is a real directory, so the plan path resolves."""
    ticket = Ticket.objects.create(overlay="test", issue_url=_ISSUE_URL)
    checkout = tmp / "checkout"
    checkout.mkdir(exist_ok=True)
    Worktree.objects.create(
        ticket=ticket,
        overlay="test",
        repo_path=_E2E_REPO,
        branch="8521-feat-thing",
        extra={"worktree_path": str(checkout)},
    )
    return ticket


class TestNoVideoGateAtCommand(TestCase):
    """``build_validated_write`` refuses a stills-only manifest unless ``--allow-no-video``."""

    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._tmp = tmp_path
        monkeypatch.setattr(
            _write,
            "resolve_worktree",
            MagicMock(side_effect=_write.WorktreeNotFoundError("none")),
        )
        self.enterContext(patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY))

    def _ticket(self) -> None:
        _seed_ticket_with_e2e_worktree(self._tmp)

    def _manifest(self, *, video: bool) -> str:
        local: dict[str, object] = {"images": [str(_red_boxed_png(self._tmp / "a.png"))]}
        if video:
            local["video"] = _real_video(self._tmp / "v.mp4")
        return json.dumps(
            {
                "ticket": "8521",
                "local": {"commits": {"client": "aabb"}},
                "workflows": [{"workflow": "Login", "local": local}],
            },
        )

    def test_stills_only_manifest_is_refused(self) -> None:
        self._ticket()
        flags = _write.TestPlanFlags(ticket="", manifest=self._manifest(video=False))
        with pytest.raises(_test_plan.TestPlanValidationError, match="no video"):
            _write.build_validated_write(flags)

    def test_stills_only_manifest_passes_with_allow_no_video(self) -> None:
        self._ticket()
        flags = _write.TestPlanFlags(ticket="", manifest=self._manifest(video=False), allow_no_video=True)
        write = _write.build_validated_write(flags)
        assert write.issue_url == _ISSUE_URL

    @pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
    def test_manifest_with_a_video_passes(self) -> None:
        self._ticket()
        flags = _write.TestPlanFlags(ticket="", manifest=self._manifest(video=True))
        write = _write.build_validated_write(flags)
        assert write.issue_url == _ISSUE_URL


def _blank_preroll_webm(path: Path) -> str:
    """Render a REAL video that opens with ~8s of solid-black pre-roll, then motion.

    This is the recurrence under test: a recording the author started long before
    the interaction began, so the post path's video-evidence gate must refuse it.
    Returns the path as a string for a manifest ``video`` slot.
    """
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    black = path.with_name("black.mp4")
    moving = path.with_name("moving.mp4")
    sources = (
        ("color=c=black:size=160x120:rate=10", black, 8.0),
        ("testsrc=size=160x120:rate=10", moving, 4.0),
    )
    for src, dst, dur in sources:
        subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i", f"{src}:duration={dur}", "-pix_fmt", "yuv420p", str(dst)],
            check=True,
            capture_output=True,
        )
    concat = path.with_name("concat.txt")
    concat.write_text(f"file '{black}'\nfile '{moving}'\n", encoding="utf-8")
    subprocess.run(
        [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(path)],
        check=True,
        capture_output=True,
    )
    return str(path)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)
class TestVideoPrerollGateAtCommand(TestCase):
    """``build_validated_write`` REFUSES a manifest whose video opens with blank pre-roll."""

    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._tmp = tmp_path
        monkeypatch.setattr(
            _write,
            "resolve_worktree",
            MagicMock(side_effect=_write.WorktreeNotFoundError("none")),
        )
        self.enterContext(patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY))

    def _ticket(self) -> None:
        _seed_ticket_with_e2e_worktree(self._tmp)

    def _manifest(self, video_path: str) -> str:
        return json.dumps(
            {
                "ticket": "8521",
                "local": {"commits": {"client": "aabb"}},
                "workflows": [
                    {
                        "workflow": "Login",
                        "local": {"images": [str(_red_boxed_png(self._tmp / "a.png"))], "video": video_path},
                    }
                ],
            },
        )

    def test_blank_preroll_video_is_refused(self) -> None:
        self._ticket()
        video = _blank_preroll_webm(self._tmp / "blank.mp4")
        flags = _write.TestPlanFlags(ticket="", manifest=self._manifest(video))
        with pytest.raises(_test_plan.TestPlanValidationError, match=r"(?i)pre-roll"):
            _write.build_validated_write(flags)

    def test_skip_validation_lets_a_blank_preroll_video_through(self) -> None:
        self._ticket()
        video = _blank_preroll_webm(self._tmp / "blank2.mp4")
        flags = _write.TestPlanFlags(ticket="", manifest=self._manifest(video), skip_validation=True)
        write = _write.build_validated_write(flags)
        assert write.issue_url == _ISSUE_URL

    def test_tight_video_passes(self) -> None:
        self._ticket()
        tight = self._tmp / "tight.mp4"
        subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=160x120:rate=10:duration=8",
                "-pix_fmt",
                "yuv420p",
                str(tight),
            ],
            check=True,
            capture_output=True,
        )
        flags = _write.TestPlanFlags(ticket="", manifest=self._manifest(str(tight)))
        write = _write.build_validated_write(flags)
        assert write.issue_url == _ISSUE_URL


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
            _write,
            "resolve_worktree",
            MagicMock(side_effect=_write.WorktreeNotFoundError("none")),
        )
        self.enterContext(patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY))

    def test_manifest_ticket_field_used_when_flag_omitted(self) -> None:
        _seed_ticket_with_e2e_worktree(self._tmp)
        img = _red_boxed_png(self._tmp / "a.png")
        manifest = json.dumps(
            {
                "ticket": "8521",
                "local": {"commits": {"client": "aabb"}},
                "workflows": [{"workflow": "Login", "local": {"images": [str(img)]}}],
            },
        )
        flags = _write.TestPlanFlags(ticket="", manifest=manifest, allow_no_video=True)
        write = _write.build_validated_write(flags)
        assert write.issue_url == _ISSUE_URL

    def test_missing_ticket_everywhere_raises_resolution_error(self) -> None:
        img = _red_boxed_png(self._tmp / "a.png")
        manifest = json.dumps(
            {
                "local": {"commits": {"client": "aabb"}},
                "workflows": [{"workflow": "Login", "local": {"images": [str(img)]}}],
            },
        )
        flags = _write.TestPlanFlags(ticket="", manifest=manifest, allow_no_video=True)
        with pytest.raises(_write.TestPlanResolutionError, match="Could not determine the ticket"):
            _write.build_validated_write(flags)


class TestMrLabel:
    """The MR link rendering is a pure helper."""

    def test_gitlab_mr_renders_repo_bang_num(self) -> None:
        line = _render.render_mrs_line(("https://gitlab.com/grp/sub/client/-/merge_requests/6331",))
        assert line == "Repos & MRs: [client!6331](https://gitlab.com/grp/sub/client/-/merge_requests/6331)"

    def test_github_pr_renders_repo_hash_num(self) -> None:
        line = _render.render_mrs_line(("https://github.com/owner/product/pull/7585",))
        assert line == "Repos & MRs: [product#7585](https://github.com/owner/product/pull/7585)"

    def test_non_url_ref_shown_verbatim(self) -> None:
        line = _render.render_mrs_line(("client!6331",))
        assert line == "Repos & MRs: client!6331"


# --- command + file-store integration ---------------------------------------


class _PlanFileTestBase(TestCase):
    """A ticket whose e2e-repo worktree is a real directory the plan can land in."""

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        self._tmp = tmp_path

    def _ticket(self) -> Ticket:
        return _seed_ticket_with_e2e_worktree(self._tmp)

    @property
    def _plan_path(self) -> Path:
        return self._tmp / "checkout" / "test-plans" / "repo-8521.md"

    def _run(self, **kwargs: object) -> dict[str, object]:
        self._monkeypatch.setattr(_write, "_resolve_worktree_or_none", lambda: None)
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_E2E_OVERLAY):
            return cast("dict[str, object]", call_command("e2e", "write-test-plan", **kwargs))

    def _run_expecting_exit(self, **kwargs: object) -> None:
        with pytest.raises(SystemExit):
            self._run(**kwargs)

    def _local_manifest(self) -> str:
        img = str(_red_boxed_png(self._tmp / "step1.png"))
        return json.dumps(
            {
                "ticket": "8521",
                "mrs": ["https://gitlab.com/org/client/-/merge_requests/6331"],
                "local": {"commits": {"client": "aabb"}, "ran_at": "2026-08-13T09:00:00Z"},
                "workflows": [{"workflow": "Login", "local": {"images": [img]}}],
            },
        )

    def _run_local(self, **kwargs: object) -> dict[str, object]:
        return self._run(ticket=_ISSUE_URL, manifest=self._local_manifest(), allow_no_video=True, **kwargs)


class TestWritesThePlanFile(_PlanFileTestBase):
    """A first run creates ``test-plans/<repo>-<ticket>.md`` and cites its captures."""

    def test_creates_the_plan_file_at_the_derived_path(self) -> None:
        self._ticket()

        result = self._run_local()

        assert result["action"] == "created"
        assert result["envs"] == ["local"]
        assert result["path"] == str(self._plan_path)
        body = self._plan_path.read_text(encoding="utf-8")
        assert "<!-- t3-e2e-evidence ticket=8521 -->" in body
        assert "`step1.png`" in body
        assert "2026-08-13T09:00:00Z" in body

    def test_the_plan_file_carries_no_host_absolute_capture_path(self) -> None:
        self._ticket()

        self._run_local()

        assert str(self._tmp) not in self._plan_path.read_text(encoding="utf-8")

    def test_second_run_updates_the_same_file_rather_than_adding_another(self) -> None:
        self._ticket()
        self._run_local()

        result = self._run_local()

        assert result["action"] == "updated"
        assert [p.name for p in self._plan_path.parent.iterdir()] == ["repo-8521.md"]
        assert self._plan_path.read_text(encoding="utf-8").count("<!-- t3-e2e-evidence ticket=8521 -->") == 1

    def test_a_dev_run_merges_over_the_local_run_already_recorded(self) -> None:
        self._ticket()
        self._run_local()
        dev_manifest = json.dumps(
            {
                "ticket": "8521",
                "dev": {"commits": {"client": "ccdd"}, "ran_at": "2026-08-14T11:30:00Z"},
                "workflows": [{"workflow": "Login", "steps": ["Open the app"]}],
            },
        )

        self._run(ticket=_ISSUE_URL, manifest=dev_manifest)

        body = self._plan_path.read_text(encoding="utf-8")
        assert "aabb" in body
        assert "ccdd" in body
        assert "2026-08-13T09:00:00Z" in body
        assert "2026-08-14T11:30:00Z" in body


class TestNothingIsPosted(_PlanFileTestBase):
    """The plan is a file: writing it makes no forge call at all."""

    def test_no_code_host_is_resolved_or_called(self) -> None:
        self._ticket()
        host = MagicMock()
        with patch("teatree.core.backend_factory.code_host_from_overlay", return_value=host) as factory:
            self._run_local()

        factory.assert_not_called()
        assert host.method_calls == []

    def test_the_command_no_longer_exposes_a_posting_verb(self) -> None:
        assert not hasattr(E2eCommand, "post_test_plan")
        assert not hasattr(E2eCommand, "post_evidence")
        assert not hasattr(E2eCommand, "retract_evidence")


class TestUnresolvablePlanLocation(_PlanFileTestBase):
    """A ticket with no e2e-repo worktree fails loud instead of writing nowhere."""

    def test_missing_e2e_worktree_exits_nonzero(self) -> None:
        Ticket.objects.create(overlay="test", issue_url=_ISSUE_URL)
        with pytest.raises(SystemExit):
            self._run_local()
        assert not self._plan_path.exists()


class TestCapturePreflightAtCommand(_PlanFileTestBase):
    """The red-box preflight refuses a no-red-box screenshot before anything is written."""

    def _no_red_box_manifest(self) -> str:
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

    def test_no_red_box_refused_before_the_write(self) -> None:
        self._ticket()
        self._run_expecting_exit(ticket=_ISSUE_URL, manifest=self._no_red_box_manifest())
        assert not self._plan_path.exists()

    def test_skip_validation_lets_a_no_red_box_plan_through(self) -> None:
        self._ticket()
        self._run(ticket=_ISSUE_URL, manifest=self._no_red_box_manifest(), skip_validation=True, allow_no_video=True)
        assert self._plan_path.is_file()

    def test_missing_artifact_file_exits_before_the_write(self) -> None:
        self._ticket()
        manifest = json.dumps(
            {
                "ticket": "8521",
                "local": {"commits": {}},
                "workflows": [{"workflow": "Login", "local": {"images": [str(self._tmp / "absent.png")]}}],
            },
        )
        self._run_expecting_exit(ticket=_ISSUE_URL, manifest=manifest)
        assert not self._plan_path.exists()


class TestRequiresManifest(_PlanFileTestBase):
    """An empty --manifest exits non-zero rather than writing an empty plan."""

    def test_empty_manifest_exits_nonzero(self) -> None:
        self._ticket()
        self._run_expecting_exit(ticket=_ISSUE_URL, manifest="")
        assert not self._plan_path.exists()


class TestBlockedBodyGateAtCommand(_PlanFileTestBase):
    """A "could not test" free-text plan is refused — the file ships to colleagues in the MR."""

    def test_blocked_phrase_in_a_body_file_is_refused_with_nothing_written(self) -> None:
        self._ticket()
        body = self._tmp / "plan.md"
        body.write_text("## Test Plan\n\nUnable to test the login flow.\n", encoding="utf-8")
        with patch(
            "teatree.core.management.commands._test_plan.write.check_blocked_body_from_config",
            side_effect=BlockedTestPlanPostError("blocked phrase"),
        ):
            self._run_expecting_exit(ticket=_ISSUE_URL, body_file=str(body))
        assert not self._plan_path.exists()

    def test_a_structured_blocked_workflow_disclosure_is_not_gated(self) -> None:
        self._ticket()
        manifest = json.dumps(
            {
                "ticket": "8521",
                "local": {"commits": {"client": "aabb"}},
                "workflows": [{"workflow": "Login", "steps": ["Open the app"]}],
                "blocked_workflows": {"Login": "deploy blocked on cred"},
            },
        )

        self._run(ticket=_ISSUE_URL, manifest=manifest)

        assert "**Blocked:** deploy blocked on cred" in self._plan_path.read_text(encoding="utf-8")


class TestPureHelpers:
    """The marker / state-blob / existing-note helpers are independently testable."""

    def test_marker_round_trip(self) -> None:
        marker = _render.render_ticket_marker(ticket_id="8521")
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
        found = _mr_post.find_existing_note(comments, ticket_id="8521")
        assert found is not None
        assert found.comment_id == 3
        assert found.state["ticket"] == "8521"
        assert _mr_post.find_existing_note([], ticket_id="8521") is None


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


# --- #2304: templates, never-render-empty, --body-file ----------------------


class TestBrowserClickFirstTemplate(TestCase):
    def _state(self, *, steps: list[str] | None = None) -> PlanState:
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

    def _steps_only_state(self) -> PlanState:
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
        state: PlanState = {
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
    def _state(self) -> PlanState:
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

    def _steps_only_state(self) -> PlanState:
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
        state: PlanState = {
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


class TestScenarioPlanTemplate(TestCase):
    """The ``scenario-plan`` template renders the hand-authored exemplar shape.

    Each scenario is a Preconditions / numbered Steps / Expected / Actual block
    (with a ``✅`` pass marker), captioned inline screenshots for a UI scenario
    or an API-contract block for an ``api`` scenario, ``---`` separators between
    scenarios, and an ``**Environment:**`` footer.
    """

    def _ui_scenario(self, **over: object) -> _scenario.Scenario:
        scenario: _scenario.Scenario = {
            "surface": "Settings page",
            "title": "Toggle dark mode",
            "preconditions": "Logged in as a verified user.",
            "steps": ["Open the settings page", "Click Dark mode", "Confirm"],
            "expected": "The theme switches to dark.",
            "modality": "ui",
            "actual_pass": True,
            "images": [{"slot": "settings", "caption": "Dark theme applied", "image_md": "![](/uploads/s/a.png)"}],
        }
        scenario.update(over)
        return scenario

    def _state(self, *, scenarios: list[_scenario.Scenario], intro: str = "", environment: str = "") -> PlanState:
        state: PlanState = {
            "ticket": "1025",
            "title": "Dark mode toggle",
            "mrs": [],
            "dev": _empty_side(env="dev"),
            "local": _empty_side(env="local"),
            "steps": {},
            "template": "scenario-plan",
            "scenarios": scenarios,
        }
        if intro:
            state["scenario_intro"] = intro
        if environment:
            state["environment"] = environment
        return state

    def test_renders_scenario_heading_with_surface(self) -> None:
        body = _render.render_body(self._state(scenarios=[self._ui_scenario()]))
        assert "### Scenario 1 — Settings page" in body

    def test_renders_preconditions_steps_expected_blocks(self) -> None:
        body = _render.render_body(self._state(scenarios=[self._ui_scenario()]))
        visible = body.split("-->")[-1]
        assert "**Preconditions:** Logged in as a verified user." in visible
        assert "**Steps:**" in visible
        assert "1. Open the settings page" in visible
        assert "2. Click Dark mode" in visible
        assert "3. Confirm" in visible
        assert "**Expected:** The theme switches to dark." in visible

    def test_passing_actual_renders_check_pass(self) -> None:
        body = _render.render_body(self._state(scenarios=[self._ui_scenario()]))
        assert "**Actual:** ✅ Pass." in body

    def test_non_passing_actual_renders_blocked_marker(self) -> None:
        scenario = self._ui_scenario(actual_pass=False, actual_note="Blocked: feature flag off on dev.")
        body = _render.render_body(self._state(scenarios=[scenario]))
        visible = body.split("-->")[-1]
        assert "✅ Pass." not in visible
        assert "Blocked: feature flag off on dev." in visible

    def test_captioned_inline_images_render(self) -> None:
        body = _render.render_body(self._state(scenarios=[self._ui_scenario()]))
        visible = body.split("-->")[-1]
        assert "*Dark theme applied*" in visible
        assert "![](/uploads/s/a.png)" in visible

    def test_api_scenario_renders_no_screenshot_block(self) -> None:
        contract = self._ui_scenario(modality="api", images=[])
        body = _render.render_body(self._state(scenarios=[contract]))
        visible = body.split("-->")[-1]
        assert "contract check — no screenshot" in visible

    def test_scenarios_separated_by_horizontal_rule(self) -> None:
        body = _render.render_body(
            self._state(scenarios=[self._ui_scenario(), self._ui_scenario(surface="Profile page")])
        )
        assert "### Scenario 2 — Profile page" in body
        # One `---` separates the two scenarios.
        assert body.count("\n---\n") >= 1

    def test_environment_footer_renders(self) -> None:
        body = _render.render_body(
            self._state(scenarios=[self._ui_scenario()], environment="dev.example.com @ commit abcd1234")
        )
        assert "**Environment:** dev.example.com @ commit abcd1234" in body

    def test_optional_intro_renders_above_first_scenario(self) -> None:
        body = _render.render_body(
            self._state(scenarios=[self._ui_scenario()], intro="Verified the toggle flow end to end.")
        )
        visible = body.split("-->")[-1]
        intro_at = visible.index("Verified the toggle flow end to end.")
        first_scenario_at = visible.index("### Scenario 1")
        assert intro_at < first_scenario_at

    def test_header_marker_and_title_still_render(self) -> None:
        body = _render.render_body(self._state(scenarios=[self._ui_scenario()]))
        assert "<!-- t3-e2e-evidence ticket=1025 -->" in body
        assert "## Test Plan — Dark mode toggle" in body

    def test_state_round_trips_scenarios_through_coerce(self) -> None:
        state = self._state(scenarios=[self._ui_scenario()], intro="Intro line.", environment="dev.example.com")
        recovered = _render.coerce_state(json.loads(json.dumps(state)))
        assert recovered["template"] == "scenario-plan"
        assert recovered["scenarios"][0]["surface"] == "Settings page"
        assert recovered["scenarios"][0]["steps"] == ["Open the settings page", "Click Dark mode", "Confirm"]
        assert recovered["scenarios"][0]["images"][0]["caption"] == "Dark theme applied"
        assert recovered["scenario_intro"] == "Intro line."
        assert recovered["environment"] == "dev.example.com"

    def test_known_templates_includes_scenario_plan(self) -> None:
        assert "scenario-plan" in _render.KNOWN_TEMPLATES


class TestNeverEmptyRender(TestCase):
    def test_raises_on_empty_state(self) -> None:
        state: PlanState = {
            "ticket": "8521",
            "title": "Empty",
            "mrs": [],
            "dev": _empty_side(env="dev"),
            "local": _empty_side(env="local"),
            "steps": {},
        }
        with pytest.raises(_render.TestPlanValidationError, match="empty"):
            _render.render_body(state)


class TestBodyFile(_PlanFileTestBase):
    """``--body-file`` writes a pre-authored body verbatim to the ticket's plan file."""

    def _body_file(self, content: str) -> str:
        path = self._tmp / "plan.md"
        path.write_text(content, encoding="utf-8")
        return str(path)

    def test_body_file_content_lands_in_the_plan_file(self) -> None:
        self._ticket()
        body = "<!-- t3-e2e-evidence ticket=8521 -->\n## Test Plan\n\nSome steps.\n"

        self._run(ticket=_ISSUE_URL, body_file=self._body_file(body))

        assert self._plan_path.read_text(encoding="utf-8") == body

    def test_empty_body_file_exits_nonzero(self) -> None:
        self._ticket()
        self._run_expecting_exit(ticket=_ISSUE_URL, body_file=self._body_file(""))
        assert not self._plan_path.exists()

    def test_body_file_and_manifest_mutually_exclusive(self) -> None:
        self._ticket()
        self._run_expecting_exit(
            ticket=_ISSUE_URL,
            body_file=self._body_file("## Plan\n"),
            manifest='{"workflows":[]}',
        )


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
    """A second ``write-test-plan`` re-reads the blob; new fields must survive."""

    def _seeded_state(self) -> PlanState:
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

    def _reread(self, state: PlanState) -> PlanState:
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
        state: PlanState = {
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
