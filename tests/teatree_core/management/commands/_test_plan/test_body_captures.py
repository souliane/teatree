"""Tests for the captures a hand-authored ``--body-file`` plan links to.

A body names its captures itself, as links into ``evidence/<plan>/<side>/``. Written
verbatim, those links pointed at files nothing had copied or checked: ``--embed-captures``
and ``--artifacts-dir`` were accepted and ignored. These tests pin the body path to the
manifest path's embedding and gates.
"""

import shutil
from pathlib import Path

import pytest
from django.core.management import call_command

from teatree.core.evidence.bdd_scenario_source import render_bdd_source
from teatree.core.management.commands._test_plan.body_captures import resolve_body_captures
from teatree.core.management.commands._test_plan.state import TestPlanValidationError
from teatree.utils.media import MediaKind
from tests.teatree_core.e2e_command.test_write_test_plan import _blank_preroll_webm, _real_video
from tests.teatree_core.management.commands._test_plan.test_committed_captures import (
    _FINAL_BDD_SOURCE,
    _ISSUE_URL,
    _plain_png,
    _PlanCaptureTestBase,
    _red_boxed_png,
)

_NEEDS_FFMPEG = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


class _BodyCaptureTestBase(_PlanCaptureTestBase):
    @property
    def _artifacts(self) -> Path:
        return self._tmp / "artifacts"

    def _artifact(self, relative: str, *, valid: bool = True, fill: tuple[int, int, int] = (245, 245, 245)) -> Path:
        path = self._artifacts / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        return _red_boxed_png(path, fill=fill) if valid else _plain_png(path)

    def _body(self, *links: str) -> str:
        path = self._tmp / "plan.md"
        images = "\n".join(f"![{Path(link).stem}]({link})" for link in links)
        source = render_bdd_source(_FINAL_BDD_SOURCE)
        path.write_text(f"{source}\n\n## Test Plan\n\n1. Open the search.\n\n{images}\n", encoding="utf-8")
        return str(path)

    def _write_body(self, body_file: str, **kwargs: object) -> None:
        call_command("e2e", "write-test-plan", ticket=_ISSUE_URL, body_file=body_file, **kwargs)

    def _embed(self, body_file: str, **kwargs: object) -> None:
        options = {"embed_captures": True, "artifacts_dir": str(self._artifacts), "allow_no_video": True}
        self._write_body(body_file, **options | kwargs)


class TestBodyLinksAreEmbeddedFromTheArtifactsDir(_BodyCaptureTestBase):
    def test_linked_captures_are_copied_beside_the_plan_and_the_body_is_written_verbatim(self) -> None:
        self._ticket()
        local = self._artifact("local/scenario-1/search-result.png")
        stack = self._artifact("stack/scenario-1/search-result.png", fill=(210, 230, 250))
        body = self._body(
            "evidence/repo-4521/local/search-result.png",
            "evidence/repo-4521/stack/search-result.png",
        )

        self._embed(body)

        assert (self._evidence_dir / "local" / "search-result.png").read_bytes() == local.read_bytes()
        assert (self._evidence_dir / "stack" / "search-result.png").read_bytes() == stack.read_bytes()
        assert self._plan_path.read_text(encoding="utf-8") == Path(body).read_text(encoding="utf-8")

    def test_a_capture_the_artifacts_dir_lacks_is_refused_and_nothing_is_written(self) -> None:
        self._ticket()
        self._artifact("local/present.png")
        body = self._body("evidence/repo-4521/local/present.png", "evidence/repo-4521/local/absent.png")

        with pytest.raises(SystemExit):
            self._embed(body)

        assert not self._plan_path.exists()
        assert not self._evidence_dir.exists()

    def test_a_linked_capture_with_no_red_box_is_refused_before_anything_is_copied(self) -> None:
        self._ticket()
        self._artifact("local/unboxed.png", valid=False)

        with pytest.raises(SystemExit):
            self._embed(self._body("evidence/repo-4521/local/unboxed.png"))

        assert not self._plan_path.exists()
        assert not self._evidence_dir.exists()

    def test_byte_identical_captures_on_two_sides_are_refused(self) -> None:
        self._ticket()
        self._artifact("local/result.png")
        self._artifact("stack/result.png")

        with pytest.raises(SystemExit):
            self._embed(self._body("evidence/repo-4521/local/result.png", "evidence/repo-4521/stack/result.png"))

        assert not self._plan_path.exists()

    def test_a_stills_only_body_needs_allow_no_video(self) -> None:
        self._ticket()
        self._artifact("local/result.png")

        with pytest.raises(SystemExit):
            self._embed(self._body("evidence/repo-4521/local/result.png"), allow_no_video=False)

        assert not self._plan_path.exists()

    def test_an_already_committed_stale_capture_is_replaced_by_the_fresh_one(self) -> None:
        self._ticket()
        (self._evidence_dir / "local").mkdir(parents=True)
        _plain_png(self._evidence_dir / "local" / "result.png")
        fresh = self._artifact("local/result.png")

        self._embed(self._body("evidence/repo-4521/local/result.png"))

        assert (self._evidence_dir / "local" / "result.png").read_bytes() == fresh.read_bytes()


class TestLegacyFlatCapturesUnderABody(_BodyCaptureTestBase):
    def test_a_flat_capture_the_body_no_longer_links_gives_way_to_its_side_capture(self) -> None:
        self._ticket()
        fresh = self._artifact("local/result.png")
        self._evidence_dir.mkdir(parents=True)
        legacy = self._evidence_dir / "result.png"
        shutil.copyfile(fresh, legacy)

        self._embed(self._body("evidence/repo-4521/local/result.png"))

        assert not legacy.exists()
        assert (self._evidence_dir / "local" / "result.png").read_bytes() == fresh.read_bytes()

    def test_a_flat_capture_the_body_still_links_stays_in_place(self) -> None:
        self._ticket()
        self._evidence_dir.mkdir(parents=True)
        legacy = _red_boxed_png(self._evidence_dir / "result.png", fill=(200, 220, 240))
        legacy_bytes = legacy.read_bytes()
        self._artifact("local/result.png")

        self._embed(self._body("evidence/repo-4521/result.png", "evidence/repo-4521/local/result.png"))

        assert legacy.read_bytes() == legacy_bytes
        assert (self._evidence_dir / "local" / "result.png").is_file()


class TestBodyLinksWithoutEmbedding(_BodyCaptureTestBase):
    def test_a_link_to_an_uncommitted_capture_is_refused(self) -> None:
        self._ticket()
        self._artifact("local/result.png")

        with pytest.raises(SystemExit):
            self._write_body(self._body("evidence/repo-4521/local/result.png"), allow_no_video=True)

        assert not self._plan_path.exists()

    def test_a_link_to_a_committed_capture_is_written(self) -> None:
        self._ticket()
        (self._evidence_dir / "local").mkdir(parents=True)
        _red_boxed_png(self._evidence_dir / "local" / "result.png")
        body = self._body("evidence/repo-4521/local/result.png")

        self._write_body(body, allow_no_video=True)

        assert self._plan_path.read_text(encoding="utf-8") == Path(body).read_text(encoding="utf-8")

    def test_a_body_without_evidence_links_is_written_verbatim_even_with_embed_captures(self) -> None:
        self._ticket()
        body = self._body()

        self._write_body(body, embed_captures=True)

        assert self._plan_path.read_text(encoding="utf-8") == Path(body).read_text(encoding="utf-8")
        assert not self._evidence_dir.exists()


@_NEEDS_FFMPEG
class TestLinkedVideosAreGated(_BodyCaptureTestBase):
    def test_a_linked_video_with_blank_pre_roll_is_refused(self) -> None:
        self._ticket()
        self._artifact("local/result.png")
        (self._artifacts / "local").mkdir(parents=True, exist_ok=True)
        _blank_preroll_webm(self._artifacts / "local" / "journey.mp4")

        with pytest.raises(SystemExit):
            self._embed(
                self._body("evidence/repo-4521/local/result.png", "evidence/repo-4521/local/journey.mp4"),
                allow_no_video=False,
            )

        assert not self._plan_path.exists()

    def test_a_linked_video_is_copied_and_satisfies_the_stills_only_gate(self) -> None:
        self._ticket()
        self._artifact("local/result.png")
        (self._artifacts / "local").mkdir(parents=True, exist_ok=True)
        video = Path(_real_video(self._artifacts / "local" / "journey.mp4"))

        self._embed(
            self._body("evidence/repo-4521/local/result.png", "evidence/repo-4521/local/journey.mp4"),
            allow_no_video=False,
        )

        assert (self._evidence_dir / "local" / "journey.mp4").read_bytes() == video.read_bytes()


class TestResolveBodyCaptures:
    def _evidence_dir(self, tmp_path: Path) -> Path:
        return tmp_path / "test-plans" / "evidence" / "repo-4521"

    def test_links_in_image_link_and_html_forms_resolve_once_each(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        (artifacts / "local").mkdir(parents=True)
        source = _red_boxed_png(artifacts / "local" / "a.png")
        body = (
            "![a](evidence/repo-4521/local/a.png)\n"
            "[again](./evidence/repo-4521/local/a.png)\n"
            '<img src="evidence/repo-4521/local/a.png" width="400">\n'
            "![remote](https://example.com/evidence/repo-4521/local/b.png)\n"
        )

        captures = resolve_body_captures(
            body, evidence_dir=self._evidence_dir(tmp_path), embed=True, artifacts_dir=artifacts
        )

        assert captures.incoming == {"local": [source]}
        assert captures.linked(MediaKind.IMAGE) == [source]

    def test_an_ambiguous_capture_names_every_match(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        for run in ("run-1", "run-2"):
            (artifacts / run / "local").mkdir(parents=True)
            _red_boxed_png(artifacts / run / "local" / "a.png")

        with pytest.raises(TestPlanValidationError, match=r"(?s)ambiguous.*run-1.*run-2"):
            resolve_body_captures(
                "![a](evidence/repo-4521/local/a.png)",
                evidence_dir=self._evidence_dir(tmp_path),
                embed=True,
                artifacts_dir=artifacts,
            )

    def test_a_capture_under_another_side_does_not_satisfy_the_link(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        (artifacts / "dev").mkdir(parents=True)
        _red_boxed_png(artifacts / "dev" / "a.png")

        with pytest.raises(TestPlanValidationError, match="not found"):
            resolve_body_captures(
                "![a](evidence/repo-4521/local/a.png)",
                evidence_dir=self._evidence_dir(tmp_path),
                embed=True,
                artifacts_dir=artifacts,
            )

    def test_a_link_into_another_plans_evidence_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(TestPlanValidationError, match="evidence/repo-4521/"):
            resolve_body_captures(
                "![a](evidence/4521/local/a.png)",
                evidence_dir=self._evidence_dir(tmp_path),
                embed=False,
                artifacts_dir=None,
            )

    def test_a_link_under_an_unknown_side_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(TestPlanValidationError, match="dev, local or stack"):
            resolve_body_captures(
                "![a](evidence/repo-4521/qa/a.png)",
                evidence_dir=self._evidence_dir(tmp_path),
                embed=False,
                artifacts_dir=None,
            )

    def test_embedding_with_no_artifacts_dir_names_the_missing_flag(self, tmp_path: Path) -> None:
        with pytest.raises(TestPlanValidationError, match="--artifacts-dir"):
            resolve_body_captures(
                "![a](evidence/repo-4521/local/a.png)",
                evidence_dir=self._evidence_dir(tmp_path),
                embed=True,
                artifacts_dir=None,
            )

    def test_a_percent_encoded_link_resolves_to_its_file_name(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        (artifacts / "local").mkdir(parents=True)
        source = _red_boxed_png(artifacts / "local" / "search result.png")

        captures = resolve_body_captures(
            "![a](evidence/repo-4521/local/search%20result.png)",
            evidence_dir=self._evidence_dir(tmp_path),
            embed=True,
            artifacts_dir=artifacts,
        )

        assert captures.incoming == {"local": [source]}
