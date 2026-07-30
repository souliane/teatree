"""Tests for the ``--manifest`` parse + validation (``_test_plan/manifest.py``).

The manifest gate is the last thing standing between a malformed capture run
and a public note: every invalid shape must raise
:class:`TestPlanValidationError` BEFORE any upload. These cases pin the guard
paths that need no on-disk media (bad JSON, empty/absent captures, unknown
template) plus a real-file happy path under ``tmp_path`` (Unit 22 split).
"""

import json
from pathlib import Path

import pytest

from teatree.core.management.commands._test_plan import state as state_mod
from teatree.core.management.commands._test_plan.manifest import parse_manifest, validate_template
from teatree.core.management.commands._test_plan.state import DEFAULT_TEMPLATE

# A minimal valid PNG (8-byte signature + IHDR) that ``media_kind`` recognises.
_PNG_BYTES = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489")


class TestParseManifestGuards:
    def test_invalid_json_raises(self) -> None:
        with pytest.raises(state_mod.TestPlanValidationError, match="not valid JSON"):
            parse_manifest("{not json")

    def test_non_object_raises(self) -> None:
        with pytest.raises(state_mod.TestPlanValidationError, match="must be a JSON object"):
            parse_manifest("[1, 2, 3]")

    def test_empty_workflows_raises(self) -> None:
        with pytest.raises(state_mod.TestPlanValidationError, match="non-empty array"):
            parse_manifest(json.dumps({"ticket": "1", "workflows": []}))

    def test_no_side_captures_raises(self) -> None:
        raw = json.dumps({"ticket": "1", "workflows": [{"workflow": "wf"}]})
        with pytest.raises(state_mod.TestPlanValidationError, match="no 'dev' or 'local' captures"):
            parse_manifest(raw)

    def test_missing_workflow_name_raises(self) -> None:
        raw = json.dumps({"ticket": "1", "dev": {}, "workflows": [{"workflow": ""}]})
        with pytest.raises(state_mod.TestPlanValidationError, match="missing a non-empty 'workflow' name"):
            parse_manifest(raw)

    def test_artifact_not_found_raises(self, tmp_path: Path) -> None:
        raw = json.dumps(
            {
                "ticket": "1",
                "workflows": [{"workflow": "wf", "dev": {"images": [str(tmp_path / "missing.png")]}}],
            }
        )
        with pytest.raises(state_mod.TestPlanValidationError, match="artifact not found"):
            parse_manifest(raw)


class TestValidateTemplate:
    def test_known_template_passes_through(self) -> None:
        assert validate_template("link-api") == "link-api"

    def test_unknown_template_raises(self) -> None:
        with pytest.raises(state_mod.TestPlanValidationError, match="must be one of"):
            validate_template("nope")


class TestParseManifestHappyPath:
    def test_steps_only_manifest_parses_without_media(self) -> None:
        raw = json.dumps(
            {
                "ticket": "8521",
                "mrs": ["https://example.com/x/-/merge_requests/1"],
                "dev": {"commits": {"repo": "abc"}},
                "workflows": [{"workflow": "login", "steps": ["open page", "click"]}],
            }
        )
        manifest = parse_manifest(raw)
        assert manifest.ticket == "8521"
        assert manifest.dev.present is True
        assert manifest.dev.commits == {"repo": "abc"}
        assert manifest.steps == {"login": ("open page", "click")}
        assert manifest.template == DEFAULT_TEMPLATE

    def test_real_image_artifact_is_accepted(self, tmp_path: Path) -> None:
        img = tmp_path / "shot.png"
        img.write_bytes(_PNG_BYTES)
        raw = json.dumps(
            {
                "ticket": "1",
                "local": {"commits": {}},
                "workflows": [{"workflow": "wf", "local": {"images": [str(img)]}}],
            }
        )
        manifest = parse_manifest(raw)
        assert manifest.local.present is True
        assert manifest.local.workflows["wf"].images == (img,)
