"""Tests for the host Claude-settings template merge (#3410, #3408).

Real temp JSON files stand in for ``~/.claude/settings.json`` and the committed
template — the merge is exercised end-to-end, nothing about the deep-merge logic
is reimplemented.
"""

import json
from pathlib import Path

import pytest

from teatree.cli.setup.claude_settings import deep_merge, managed_key_drift, write_host_claude_settings


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestDeepMerge:
    def test_override_wins_on_scalars(self) -> None:
        assert deep_merge({"a": 1, "b": 2}, {"b": 9}) == {"a": 1, "b": 9}

    def test_objects_merge_recursively(self) -> None:
        merged = deep_merge({"p": {"x": 1, "y": 2}}, {"p": {"y": 9, "z": 3}})
        assert merged == {"p": {"x": 1, "y": 9, "z": 3}}

    def test_arrays_are_replaced_not_concatenated(self) -> None:
        # jq '.[0] * .[1]' semantics: arrays are values, replaced wholesale.
        assert deep_merge({"allow": ["a", "b"]}, {"allow": ["c"]}) == {"allow": ["c"]}

    def test_inputs_are_not_mutated(self) -> None:
        base = {"p": {"x": 1}}
        deep_merge(base, {"p": {"y": 2}})
        assert base == {"p": {"x": 1}}


class TestWriteHostClaudeSettings:
    def test_creates_target_from_template_when_absent(self, tmp_path: Path) -> None:
        template = _write(tmp_path / "tpl.json", {"model": "m", "autoMode": {"allow": ["x"]}})
        target = tmp_path / "home" / ".claude" / "settings.json"
        result = write_host_claude_settings(template, target)
        assert target.is_file()
        assert result["model"] == "m"
        assert json.loads(target.read_text())["autoMode"]["allow"] == ["x"]

    def test_preserves_unmanaged_keys_and_asserts_managed(self, tmp_path: Path) -> None:
        template = _write(tmp_path / "tpl.json", {"model": "new", "autoMode": {"allow": ["managed"]}})
        target = _write(
            tmp_path / "settings.json",
            {"model": "old", "statusLine": {"type": "command"}, "autoMode": {"allow": ["user"]}},
        )
        result = write_host_claude_settings(template, target)
        # statusLine (unmanaged) survives; model + autoMode.allow (managed) win.
        assert result["statusLine"] == {"type": "command"}
        assert result["model"] == "new"
        written = json.loads(target.read_text())
        assert written["autoMode"]["allow"] == ["managed"]

    def test_missing_template_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            write_host_claude_settings(tmp_path / "nope.json", tmp_path / "out.json")


class TestManagedKeyDrift:
    def _template(self, tmp_path: Path) -> Path:
        return _write(
            tmp_path / "tpl.json",
            {
                "model": "m",
                "permissions": {"defaultMode": "acceptEdits", "allow": ["Bash(git:*)"]},
                "autoMode": {"allow": ["grant"]},
                "env": {"CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY": "4"},
            },
        )

    def test_no_drift_when_managed_keys_match(self, tmp_path: Path) -> None:
        template = self._template(tmp_path)
        target = _write(tmp_path / "t.json", {**json.loads(template.read_text()), "statusLine": {"x": 1}})
        assert managed_key_drift(template, target) == []

    def test_absent_target_drifts_every_managed_key(self, tmp_path: Path) -> None:
        template = self._template(tmp_path)
        drift = managed_key_drift(template, tmp_path / "absent.json")
        assert "model" in drift
        assert "autoMode.allow" in drift
        assert "permissions.defaultMode" in drift

    def test_reports_only_the_diverged_key(self, tmp_path: Path) -> None:
        template = self._template(tmp_path)
        data = json.loads(template.read_text())
        data["autoMode"]["allow"] = ["different"]
        target = _write(tmp_path / "t.json", data)
        assert managed_key_drift(template, target) == ["autoMode.allow"]
