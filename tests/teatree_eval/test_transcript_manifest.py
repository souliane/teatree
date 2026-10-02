"""Provenance sidecar for a captured eval transcript (#3313)."""

from pathlib import Path

from teatree.eval import transcript_manifest


def _transcript(tmp_path: Path) -> Path:
    t = tmp_path / "worktree_first.jsonl"
    t.write_text("{}\n", encoding="utf-8")
    return t


class TestVerify:
    def test_absent_manifest_is_present_false_and_ok(self, tmp_path: Path) -> None:
        # A hand-placed fixture with no sidecar grades as before (unverified).
        result = transcript_manifest.verify(_transcript(tmp_path), scenario="s", prompt="p", head_sha="abc")
        assert result.present is False
        assert result.ok is True

    def test_matching_manifest_passes(self, tmp_path: Path) -> None:
        t = _transcript(tmp_path)
        transcript_manifest.write(t, scenario="s", prompt="p", head_sha="abc", source=Path("/src/agent.jsonl"))
        result = transcript_manifest.verify(t, scenario="s", prompt="p", head_sha="abc")
        assert result.present is True
        assert result.ok is True

    def test_prompt_drift_is_refused(self, tmp_path: Path) -> None:
        t = _transcript(tmp_path)
        transcript_manifest.write(t, scenario="s", prompt="old prompt", head_sha="abc", source=Path("/x"))
        result = transcript_manifest.verify(t, scenario="s", prompt="new prompt", head_sha="abc")
        assert result.present is True
        assert result.ok is False
        assert "prompt hash mismatch" in result.reason

    def test_stale_head_is_refused(self, tmp_path: Path) -> None:
        t = _transcript(tmp_path)
        transcript_manifest.write(t, scenario="s", prompt="p", head_sha="old_sha", source=Path("/x"))
        result = transcript_manifest.verify(t, scenario="s", prompt="p", head_sha="new_sha")
        assert result.present is True
        assert result.ok is False
        assert "stale" in result.reason

    def test_scenario_mismatch_is_refused(self, tmp_path: Path) -> None:
        t = _transcript(tmp_path)
        transcript_manifest.write(t, scenario="other", prompt="p", head_sha="abc", source=Path("/x"))
        result = transcript_manifest.verify(t, scenario="s", prompt="p", head_sha="abc")
        assert result.ok is False

    def test_missing_head_on_either_side_skips_head_check(self, tmp_path: Path) -> None:
        # A capture/grade in a non-repo context records "" — the HEAD check is
        # tolerant so tmp-dir grading is not falsely refused.
        t = _transcript(tmp_path)
        transcript_manifest.write(t, scenario="s", prompt="p", head_sha="", source=Path("/x"))
        result = transcript_manifest.verify(t, scenario="s", prompt="p", head_sha="")
        assert result.ok is True

    def test_unreadable_manifest_is_refused(self, tmp_path: Path) -> None:
        t = _transcript(tmp_path)
        transcript_manifest.manifest_path(t).write_text("{not json", encoding="utf-8")
        result = transcript_manifest.verify(t, scenario="s", prompt="p", head_sha="abc")
        assert result.present is True
        assert result.ok is False
