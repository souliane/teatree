"""Tests for the tracked-manifest transform (teatree #3092).

A test-plan ``manifest.json`` mixes two lifetimes: durable authored intent
(workflow names, human ``steps``, the claim→capture mapping) and ephemeral run
provenance (per-repo commit SHAs, ``missing_on_dev``) that goes stale the moment
anything is pushed. Tracking the whole file therefore churns it every run.

``strip_run_provenance`` produces the *tracked* half — the authored manifest
with the top-level ``dev``/``local`` provenance blocks removed — so the file a
private test repo commits is stable across runs. The full manifest (with
provenance) stays out-of-repo for ``post-test-plan``.
"""

import json
import shutil
import tempfile
from pathlib import Path

import pytest
from django.test import SimpleTestCase

from teatree.core.management.commands._test_plan import render as _render
from teatree.core.management.commands._test_plan.tracked import run_tracked_manifest, strip_run_provenance


def _run_manifest(*, dev_sha: str, local_sha: str, missing: list[str]) -> str:
    """A full manifest for one run: identical authored intent, run-specific provenance."""
    return json.dumps(
        {
            "ticket": "8521",
            "mrs": ["https://gitlab.com/group/client/-/merge_requests/6331"],
            "dev": {"commits": {"client": dev_sha}, "missing_on_dev": missing},
            "local": {"commits": {"client": local_sha}},
            "workflows": [
                {
                    "workflow": "Login",
                    "steps": ["Open the app", "Click Login", "Expect the dashboard"],
                    "dev": {"video": None, "images": []},
                    "local": {"video": "local/run.webm", "images": ["local/step1.png"]},
                }
            ],
        }
    )


class StripRunProvenanceTests(SimpleTestCase):
    def test_two_runs_produce_byte_identical_tracked_manifest(self) -> None:
        first = _run_manifest(dev_sha="aaaa111", local_sha="bbbb222", missing=["client!6331 (unmerged)"])
        second = _run_manifest(dev_sha="cccc333", local_sha="dddd444", missing=["client!6331 (draft)"])

        assert strip_run_provenance(first) == strip_run_provenance(second)

    def test_tracked_manifest_carries_no_run_provenance(self) -> None:
        tracked = strip_run_provenance(_run_manifest(dev_sha="aaaa111", local_sha="bbbb222", missing=["x"]))

        for stale in ("aaaa111", "bbbb222", "commits", "missing_on_dev"):
            assert stale not in tracked
        data = json.loads(tracked)
        assert "dev" not in data
        assert "local" not in data

    def test_tracked_manifest_preserves_authored_intent(self) -> None:
        data = json.loads(strip_run_provenance(_run_manifest(dev_sha="a", local_sha="b", missing=[])))

        assert data["ticket"] == "8521"
        assert data["mrs"] == ["https://gitlab.com/group/client/-/merge_requests/6331"]
        assert data["workflows"][0]["workflow"] == "Login"
        assert data["workflows"][0]["steps"] == ["Open the app", "Click Login", "Expect the dashboard"]
        assert data["workflows"][0]["local"]["images"] == ["local/step1.png"]

    def test_tracked_manifest_still_parses_with_workflow_captures(self) -> None:
        base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(base, ignore_errors=True))
        (base / "step1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        tracked = strip_run_provenance(
            json.dumps(
                {
                    "ticket": "8521",
                    "workflows": [
                        {
                            "workflow": "Login",
                            "steps": ["Open the app"],
                            "local": {"images": ["step1.png"]},
                        }
                    ],
                    "local": {"commits": {"client": "deadbeef"}},
                }
            )
        )
        manifest = _render.parse_manifest(tracked, base_dir=base)

        assert manifest.local.present
        assert manifest.local.commits == {}
        assert list(manifest.local.workflows) == ["Login"]

    def test_invalid_json_is_rejected(self) -> None:
        with pytest.raises(_render.TestPlanValidationError):
            strip_run_provenance("{not json")

    def test_non_object_manifest_is_rejected(self) -> None:
        with pytest.raises(_render.TestPlanValidationError):
            strip_run_provenance("[]")


class RunTrackedManifestTests(SimpleTestCase):
    def test_reads_path_strips_provenance_and_writes_to_stdout(self) -> None:
        base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(base, ignore_errors=True))
        path = base / "manifest.json"
        path.write_text(_run_manifest(dev_sha="aaaa111", local_sha="bbbb222", missing=["x"]), encoding="utf-8")
        out: list[str] = []

        result = run_tracked_manifest(str(path), write_out=out.append, write_err=lambda _m: None)

        assert result == "".join(out)
        assert "aaaa111" not in result
        assert "commits" not in result

    def test_empty_manifest_exits_non_zero(self) -> None:
        errors: list[str] = []
        with pytest.raises(SystemExit):
            run_tracked_manifest("", write_out=lambda _m: None, write_err=errors.append)
        assert errors

    def test_invalid_json_exits_non_zero(self) -> None:
        errors: list[str] = []
        with pytest.raises(SystemExit):
            run_tracked_manifest("{bad", write_out=lambda _m: None, write_err=errors.append)
        assert errors
