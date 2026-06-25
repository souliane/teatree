# test-path: cross-cutting
"""Guard the removal of two provably-dead settings (souliane/teatree#2731).

Both ``branch_prefix`` and ``ask_before_post_on_behalf`` were dead surface on
``UserSettings`` — the former a DB-home field nothing read, the latter a derived
value nothing read and a deprecated shim only tests referenced. This guard turns
their removal into a mechanical floor: the field is gone from the dataclass and
the parser registry, a stored row / TOML key resolves to nothing, and nothing in
``src/`` reads the retired derived field. On-behalf gating is unaffected — it
resolves through ``resolve_on_behalf_verdict`` / ``on_behalf_post_mode``.

Integration-first per the Test-Writing Doctrine: real ``ConfigSetting`` rows and
real TOML under ``tmp_path`` asserted through ``get_effective_settings``.
"""

import dataclasses
import logging
from pathlib import Path

import pytest
from django.test import TestCase

import teatree.config as config_facade
from teatree.config import (
    OVERLAY_OVERRIDABLE_SETTINGS,
    OnBehalfPostMode,
    UserSettings,
    get_effective_settings,
    load_config,
)
from teatree.core.models import ConfigSetting
from teatree.on_behalf_gate import OnBehalfVerdict, resolve_on_behalf_verdict

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"


def _field_names() -> set[str]:
    return {f.name for f in dataclasses.fields(UserSettings)}


class TestBranchPrefixRemoved:
    """``branch_prefix`` is no longer a ``UserSettings`` field or a parser entry.

    The env-driven ``_branch_prefix()`` workspace helper (reads ``T3_BRANCH_PREFIX``
    / ``git config user.name``) is unrelated and stays — it never read this field.
    """

    def test_branch_prefix_is_not_a_user_settings_field(self) -> None:
        assert "branch_prefix" not in _field_names()

    def test_branch_prefix_not_in_overlay_overridable_registry(self) -> None:
        assert "branch_prefix" not in OVERLAY_OVERRIDABLE_SETTINGS


class TestStoredBranchPrefixResolvesToNothing(TestCase):
    """A stored ``branch_prefix`` row / ``[teatree]`` TOML key resolves to nothing.

    The key is absent from the DB-home parser registry, so a stored row supplies
    no value and the setting does not reappear on the resolved dataclass.
    """

    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        for env in ("T3_OVERLAY_NAME", "T3_BRANCH_PREFIX"):
            monkeypatch.delenv(env, raising=False)
        self.monkeypatch = monkeypatch

    def test_stored_row_is_ignored_no_attribute_reappears(self) -> None:
        ConfigSetting.objects.set_value("branch_prefix", "ac")
        settings = get_effective_settings()
        assert not hasattr(settings, "branch_prefix")

    def test_toml_key_is_ignored_on_read(self) -> None:
        self.config_path.write_text('[teatree]\nbranch_prefix = "ac"\n', encoding="utf-8")
        settings = get_effective_settings()
        assert not hasattr(settings, "branch_prefix")


class TestAskBeforePostOnBehalfRemoved:
    """The derived ``ask_before_post_on_behalf`` field and its shim are retired."""

    def test_ask_before_post_on_behalf_is_not_a_user_settings_field(self) -> None:
        assert "ask_before_post_on_behalf" not in _field_names()

    def test_resolved_settings_carry_no_ask_before_post_on_behalf(self) -> None:
        assert not hasattr(get_effective_settings(), "ask_before_post_on_behalf")

    def test_deprecated_shim_is_gone(self) -> None:
        import teatree.on_behalf_gate as gate_mod  # noqa: PLC0415

        assert not hasattr(gate_mod, "ask_before_post_on_behalf_enabled")

    def test_no_src_module_reads_the_retired_field(self) -> None:
        offenders: list[str] = []
        for path in _SRC.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if ".ask_before_post_on_behalf" in line:
                    offenders.append(f"{path.relative_to(_REPO_ROOT).as_posix()}:{lineno}: {line.strip()}")
        assert offenders == [], (
            "Retired derived field `.ask_before_post_on_behalf` is still read in src/ — "
            "use resolve_on_behalf_verdict / on_behalf_post_mode instead:\n" + "\n".join(offenders)
        )


class TestOnBehalfGatingStillResolves(TestCase):
    """Removing the dead field/shim leaves on-behalf gating fully intact."""

    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        for env in ("T3_OVERLAY_NAME", "T3_ON_BEHALF_POST_MODE", "T3_ON_BEHALF_AUTO_ACTIONS"):
            monkeypatch.delenv(env, raising=False)

    def test_default_mode_blocks_visible_posts_and_drafts_auto(self) -> None:
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK
        assert resolve_on_behalf_verdict("post_draft_note") is OnBehalfVerdict.AUTO_DRAFT

    def test_immediate_mode_resolves_to_proceed(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.PROCEED
        assert get_effective_settings().on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE


class TestRetiredKeyWarnList:
    """A leftover retired key in TOML warns rather than silently no-opping."""

    def test_branch_prefix_in_toml_warns(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text('[teatree]\nbranch_prefix = "ac"\n', encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="teatree.config.loader"):
            load_config(config_path)
        assert any("branch_prefix" in rec.message and "Retired" in rec.message for rec in caplog.records)

    def test_clean_toml_does_not_warn(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text('[teatree]\nprivacy = "strict"\n', encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="teatree.config.loader"):
            load_config(config_path)
        assert not any("Retired setting keys" in rec.message for rec in caplog.records)
