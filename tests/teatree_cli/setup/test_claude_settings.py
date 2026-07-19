"""Tests for the host Claude-settings template merge (#3410, #3408, #3437).

Real temp JSON files stand in for ``~/.claude/settings.json`` and the committed
template — the merge is exercised end-to-end, nothing about the deep-merge logic
is reimplemented. ``env`` is injected explicitly so the ``TEATREE_CLAUDE_*``
resolver is tested hermetically, independent of the ambient environment.
"""

import json
from pathlib import Path

import pytest

from teatree.cli.setup.claude_settings import (
    MANAGED_KEY_PATHS,
    _dig,
    _main,
    deep_merge,
    managed_key_drift,
    merge_host_settings,
    resolve_managed_template,
    write_host_claude_settings,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMMITTED_TEMPLATE = _REPO_ROOT / "deploy" / "claude-settings.template.json"


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
        # deep_merge stays the jq '.[0] * .[1]' primitive: arrays are values,
        # replaced wholesale. The managed allow-list union lives one layer up in
        # merge_host_settings, not here.
        assert deep_merge({"allow": ["a", "b"]}, {"allow": ["c"]}) == {"allow": ["c"]}

    def test_inputs_are_not_mutated(self) -> None:
        base = {"p": {"x": 1}}
        deep_merge(base, {"p": {"y": 2}})
        assert base == {"p": {"x": 1}}


class TestResolveManagedTemplate:
    def _template(self) -> dict[str, object]:
        return {
            "model": "base-model",
            "permissions": {"defaultMode": "acceptEdits"},
            "env": {"CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY": "4"},
        }

    def test_no_overrides_returns_equivalent_template(self) -> None:
        template = self._template()
        assert resolve_managed_template(template, {}) == template

    def test_each_override_sets_its_managed_path(self) -> None:
        resolved = resolve_managed_template(
            self._template(),
            {
                "TEATREE_CLAUDE_MODEL": "override-model",
                "TEATREE_CLAUDE_PERMISSION_MODE": "plan",
                "TEATREE_CLAUDE_TOOL_CONCURRENCY": "8",
            },
        )
        assert resolved["model"] == "override-model"
        assert _dig(resolved, ("permissions", "defaultMode")) == "plan"
        assert _dig(resolved, ("env", "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY")) == "8"

    def test_empty_override_is_ignored(self) -> None:
        resolved = resolve_managed_template(self._template(), {"TEATREE_CLAUDE_MODEL": ""})
        assert resolved["model"] == "base-model"

    def test_template_is_not_mutated(self) -> None:
        template = self._template()
        resolve_managed_template(template, {"TEATREE_CLAUDE_MODEL": "override-model"})
        assert template["model"] == "base-model"

    def test_creates_intermediate_objects_for_absent_paths(self) -> None:
        resolved = resolve_managed_template({}, {"TEATREE_CLAUDE_TOOL_CONCURRENCY": "6"})
        assert _dig(resolved, ("env", "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY")) == "6"


class TestMergeHostSettings:
    def test_operator_added_allow_entry_survives_union(self) -> None:
        base = {"permissions": {"allow": ["Bash(op:*)"]}, "autoMode": {"allow": ["op-grant"]}}
        template = {"permissions": {"allow": ["Bash(git:*)"]}, "autoMode": {"allow": ["managed-grant"]}}
        merged = merge_host_settings(base, template)
        assert _dig(merged, ("permissions", "allow")) == ["Bash(git:*)", "Bash(op:*)"]
        assert _dig(merged, ("autoMode", "allow")) == ["managed-grant", "op-grant"]

    def test_union_deduplicates_shared_entries(self) -> None:
        base = {"permissions": {"allow": ["Bash(git:*)", "Bash(op:*)"]}}
        template = {"permissions": {"allow": ["Bash(git:*)"]}}
        merged = merge_host_settings(base, template)
        assert _dig(merged, ("permissions", "allow")) == ["Bash(git:*)", "Bash(op:*)"]

    def test_non_allow_list_scalars_still_clobber(self) -> None:
        merged = merge_host_settings({"model": "old"}, {"model": "new"})
        assert merged["model"] == "new"

    def test_inputs_are_not_mutated(self) -> None:
        base = {"permissions": {"allow": ["Bash(op:*)"]}}
        template = {"permissions": {"allow": ["Bash(git:*)"]}}
        merge_host_settings(base, template)
        assert base["permissions"]["allow"] == ["Bash(op:*)"]


class TestWriteHostClaudeSettings:
    def test_creates_target_from_template_when_absent(self, tmp_path: Path) -> None:
        template = _write(tmp_path / "tpl.json", {"model": "m", "autoMode": {"allow": ["x"]}})
        target = tmp_path / "home" / ".claude" / "settings.json"
        result = write_host_claude_settings(template, target, env={})
        assert target.is_file()
        assert result["model"] == "m"
        assert json.loads(target.read_text())["autoMode"]["allow"] == ["x"]

    def test_preserves_unmanaged_keys_and_asserts_managed(self, tmp_path: Path) -> None:
        template = _write(tmp_path / "tpl.json", {"model": "new", "autoMode": {"allow": ["managed"]}})
        target = _write(
            tmp_path / "settings.json",
            {"model": "old", "statusLine": {"type": "command"}, "autoMode": {"allow": ["user"]}},
        )
        result = write_host_claude_settings(template, target, env={})
        # statusLine (unmanaged) survives; model (managed scalar) wins; the
        # managed autoMode.allow UNIONS the operator's grant with the template's.
        assert result["statusLine"] == {"type": "command"}
        assert result["model"] == "new"
        assert json.loads(target.read_text())["autoMode"]["allow"] == ["managed", "user"]

    def test_operator_added_permissions_allow_entry_survives_managed_rewrite(self, tmp_path: Path) -> None:
        template = _write(
            tmp_path / "tpl.json",
            {"permissions": {"defaultMode": "acceptEdits", "allow": ["Bash(git:*)"]}},
        )
        target = _write(
            tmp_path / "settings.json",
            {"permissions": {"defaultMode": "acceptEdits", "allow": ["Bash(git:*)", "Bash(operator-tool:*)"]}},
        )
        result = write_host_claude_settings(template, target, env={})
        # union is template-first, then operator-added extras.
        assert _dig(result, ("permissions", "allow")) == ["Bash(git:*)", "Bash(operator-tool:*)"]

    def test_teatree_claude_override_is_written(self, tmp_path: Path) -> None:
        template = _write(tmp_path / "tpl.json", {"model": "template-model"})
        target = tmp_path / "settings.json"
        result = write_host_claude_settings(template, target, env={"TEATREE_CLAUDE_MODEL": "override-model"})
        assert result["model"] == "override-model"
        assert json.loads(target.read_text())["model"] == "override-model"

    def test_missing_template_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            write_host_claude_settings(tmp_path / "nope.json", tmp_path / "out.json", env={})


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
        assert managed_key_drift(template, target, env={}) == []

    def test_absent_target_drifts_every_managed_key(self, tmp_path: Path) -> None:
        template = self._template(tmp_path)
        drift = managed_key_drift(template, tmp_path / "absent.json", env={})
        assert "model" in drift
        assert "autoMode.allow" in drift
        assert "permissions.defaultMode" in drift
        assert "permissions.allow" in drift

    def test_reports_only_the_diverged_key(self, tmp_path: Path) -> None:
        template = self._template(tmp_path)
        data = json.loads(template.read_text())
        data["autoMode"]["allow"] = ["different"]
        target = _write(tmp_path / "t.json", data)
        assert managed_key_drift(template, target, env={}) == ["autoMode.allow"]

    def test_operator_extra_allow_entry_is_not_drift(self, tmp_path: Path) -> None:
        template = self._template(tmp_path)
        data = json.loads(template.read_text())
        data["permissions"]["allow"] = ["Bash(git:*)", "Bash(operator-tool:*)"]
        data["autoMode"]["allow"] = ["grant", "extra-operator-grant"]
        target = _write(tmp_path / "t.json", data)
        # A host that is a SUPERSET of the template's grants does not drift.
        assert managed_key_drift(template, target, env={}) == []

    def test_missing_template_allow_entry_is_drift(self, tmp_path: Path) -> None:
        template = self._template(tmp_path)
        data = json.loads(template.read_text())
        data["permissions"]["allow"] = []  # operator dropped a template grant
        target = _write(tmp_path / "t.json", data)
        assert managed_key_drift(template, target, env={}) == ["permissions.allow"]

    def test_teatree_claude_override_is_honored_by_drift_check(self, tmp_path: Path) -> None:
        template = self._template(tmp_path)
        # Host agrees with the RAW template model but not the overridden one.
        target = _write(tmp_path / "t.json", json.loads(template.read_text()))
        env = {"TEATREE_CLAUDE_MODEL": "override-model"}
        assert managed_key_drift(template, target, env=env) == ["model"]
        # Host carrying the overridden value is in agreement — no drift.
        overridden = json.loads(template.read_text())
        overridden["model"] = "override-model"
        agreed = _write(tmp_path / "agreed.json", overridden)
        assert managed_key_drift(template, agreed, env=env) == []


class TestManagedKeyPathsCoverTemplate:
    def test_every_managed_path_resolves_in_committed_template(self) -> None:
        # Guards future drift: a template key renamed/removed without updating
        # MANAGED_KEY_PATHS would silently stop being asserted. Every managed path
        # must dig to a real value in the committed template.
        template = json.loads(_COMMITTED_TEMPLATE.read_text(encoding="utf-8"))
        for path in MANAGED_KEY_PATHS:
            assert _dig(template, path) is not None, f"managed path {'.'.join(path)} missing from template"


class TestTempToDiskManagedConfig:
    """The committed template routes agent/pytest temp to DISK and auto-allows temp cleanup.

    The box's ``/tmp`` is a small RAM tmpfs; agent + pytest scratch fills it to
    ENOSPC and wedges the box. The managed template pins ``TMPDIR`` /
    ``PYTEST_DEBUG_TEMPROOT`` to a disk path (so every agent's Bash tool inherits it)
    and grants a SCOPED temp-cleanup allow-list so an agent can trim ``/tmp`` without
    a classifier prompt. Both are managed, so a redeploy re-asserts them and the host
    drift check preserves them.
    """

    _TEMP_ENV_PATHS: tuple[tuple[str, ...], ...] = (("env", "TMPDIR"), ("env", "PYTEST_DEBUG_TEMPROOT"))

    def _committed(self) -> dict[str, object]:
        return json.loads(_COMMITTED_TEMPLATE.read_text(encoding="utf-8"))

    def test_temp_env_routes_off_the_tmpfs_to_disk(self) -> None:
        template = self._committed()
        for path in self._TEMP_ENV_PATHS:
            value = _dig(template, path)
            assert isinstance(value, str)
            assert value, f"{'.'.join(path)} must be a non-empty path"
            # Must NOT land on the RAM-backed /tmp tmpfs (the whole point).
            assert value == "/var/tmp"
            assert not value.startswith("/tmp/")
            assert value != "/tmp"

    def test_temp_env_paths_are_managed(self) -> None:
        for path in self._TEMP_ENV_PATHS:
            assert path in MANAGED_KEY_PATHS

    def test_temp_env_drift_is_detected(self, tmp_path: Path) -> None:
        template = _COMMITTED_TEMPLATE
        target = self._committed()
        target["env"]["TMPDIR"] = "/tmp"  # type: ignore[index]  # host diverged back onto the tmpfs
        target_path = _write(tmp_path / "t.json", target)
        assert "env.TMPDIR" in managed_key_drift(template, target_path, env={})

    def test_temp_cleanup_allow_rules_present_and_scoped(self) -> None:
        allow = _dig(self._committed(), ("permissions", "allow"))
        assert isinstance(allow, list)
        for rule in ("Bash(find /tmp:*)", "Bash(find /var/tmp:*)", "Bash(rm -rf /tmp/pytest-:*)"):
            assert rule in allow
        # Scoped, never a blanket rm/find allow.
        assert "Bash(rm:*)" not in allow
        assert "Bash(find:*)" not in allow

    def test_drift_flags_a_dropped_temp_cleanup_rule(self, tmp_path: Path) -> None:
        template = _COMMITTED_TEMPLATE
        target = self._committed()
        target["permissions"]["allow"] = ["Bash(git:*)"]  # type: ignore[index]  # operator dropped the temp grants
        target_path = _write(tmp_path / "t.json", target)
        assert "permissions.allow" in managed_key_drift(template, target_path, env={})

    def test_operator_extra_grant_beside_temp_rules_is_not_drift(self, tmp_path: Path) -> None:
        template = _COMMITTED_TEMPLATE
        target = self._committed()
        allow = target["permissions"]["allow"]  # type: ignore[index]
        assert isinstance(allow, list)
        allow.append("Bash(operator-tool:*)")  # operator addition beside every managed grant
        target_path = _write(tmp_path / "t.json", target)
        assert "permissions.allow" not in managed_key_drift(template, target_path, env={})


class TestEnabledPluginsManagedConfig:
    """The committed template enables the ``t3@souliane`` skills plugin, managed + drift-guarded.

    Factory agents load skills only when ``~/.claude/settings.json`` carries
    ``enabledPlugins: {"t3@souliane": true}``. The template pins it and it is
    managed, so every seeded container enables the plugin and the host drift check
    re-asserts it.
    """

    _PLUGIN_PATH: tuple[str, ...] = ("enabledPlugins", "t3@souliane")

    def _committed(self) -> dict[str, object]:
        return json.loads(_COMMITTED_TEMPLATE.read_text(encoding="utf-8"))

    def test_template_enables_the_t3_plugin(self) -> None:
        assert _dig(self._committed(), self._PLUGIN_PATH) is True

    def test_enabled_plugin_path_is_managed(self) -> None:
        assert self._PLUGIN_PATH in MANAGED_KEY_PATHS

    def test_drift_detected_when_plugin_disabled_on_host(self, tmp_path: Path) -> None:
        target = self._committed()
        target["enabledPlugins"]["t3@souliane"] = False  # type: ignore[index]  # host disabled the plugin
        target_path = _write(tmp_path / "t.json", target)
        assert "enabledPlugins.t3@souliane" in managed_key_drift(_COMMITTED_TEMPLATE, target_path, env={})

    def test_drift_detected_when_plugin_key_absent_on_host(self, tmp_path: Path) -> None:
        target = self._committed()
        del target["enabledPlugins"]  # host never registered the plugin
        target_path = _write(tmp_path / "t.json", target)
        assert "enabledPlugins.t3@souliane" in managed_key_drift(_COMMITTED_TEMPLATE, target_path, env={})


class TestMainScript:
    def test_renders_resolved_template_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        template = _write(tmp_path / "tpl.json", {"model": "template-model"})
        monkeypatch.setenv("TEATREE_CLAUDE_MODEL", "override-model")
        assert _main(["prog", str(template)]) == 0
        rendered = json.loads(capsys.readouterr().out)
        assert rendered["model"] == "override-model"

    def test_usage_error_on_wrong_arg_count(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert _main(["prog"]) == 2
        assert "usage" in capsys.readouterr().err

    def test_missing_template_exits_nonzero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert _main(["prog", str(tmp_path / "absent.json")]) == 1
        assert "missing or empty" in capsys.readouterr().err
