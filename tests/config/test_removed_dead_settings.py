# test-path: cross-cutting
"""Guard the removal of two provably-dead settings (souliane/teatree#2731).

Both ``branch_prefix`` and ``ask_before_post_on_behalf`` were dead surface on
``UserSettings`` — the former a DB-home field nothing read, the latter a derived
value nothing read and a deprecated shim only tests referenced. This guard turns
their removal into a mechanical floor: the field is gone from the dataclass and
the parser registry, a stored row resolves to nothing, and nothing in ``src/``
reads the retired derived field. On-behalf gating is unaffected — it resolves
through ``resolve_on_behalf_verdict`` and the active posture.

Integration-first per the Test-Writing Doctrine: real ``ConfigSetting`` rows
asserted through ``get_effective_settings``.
"""

import dataclasses
from pathlib import Path

import pytest
from django.test import TestCase

import teatree.config as config_facade
import teatree.config.loader as loader_mod
from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS, UserSettings, get_effective_settings
from teatree.config.retired_settings_ledger import RETIRED_SETTINGS
from teatree.config.setting_registries import ENV_SETTING_OVERRIDES
from teatree.core.models import ConfigSetting
from teatree.on_behalf_gate import OnBehalfVerdict, resolve_on_behalf_verdict

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
#: Where a retired key is NAMED on purpose — the roll, in both of the halves it is split across.
_LEDGER_MODULES = frozenset({"retired_settings_ledger.py", "retired_settings_loop_owned.py"})


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
    """A stored ``branch_prefix`` row resolves to nothing.

    The key is absent from the DB-home parser registry, so a stored row supplies
    no value and the setting does not reappear on the resolved dataclass.
    """

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_BRANCH_PREFIX"):
            monkeypatch.delenv(env, raising=False)

    def test_stored_row_is_ignored_no_attribute_reappears(self) -> None:
        ConfigSetting.objects.set_value("branch_prefix", "ac")
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
            "use resolve_on_behalf_verdict and the active posture instead:\n" + "\n".join(offenders)
        )


class TestWorktreesDirRemoved:
    """``worktrees_dir`` was removed as a redundant duplicate of ``worktree_root()``.

    The two resolvers shared the "where ticket worktrees are created" role with
    divergent defaults (``DATA_DIR/worktrees`` vs ``~/workspace/t3-workspaces/<overlay>``)
    and identical docstrings, so a reader could not tell which one actually created
    worktrees (config §3d #4). ``worktree_root()`` is the single canonical resolver
    now; the field, its parser entry, and the ``loader.worktrees_dir()`` accessor are gone.
    """

    def test_worktrees_dir_is_not_a_user_settings_field(self) -> None:
        assert "worktrees_dir" not in _field_names()

    def test_worktrees_dir_not_in_overlay_overridable_registry(self) -> None:
        assert "worktrees_dir" not in OVERLAY_OVERRIDABLE_SETTINGS

    def test_loader_worktrees_dir_accessor_is_gone(self) -> None:
        assert not hasattr(loader_mod, "worktrees_dir")
        assert not hasattr(config_facade, "worktrees_dir")


class TestStoredWorktreesDirResolvesToNothing(TestCase):
    """A stored ``worktrees_dir`` row supplies no value — the key is off the registry."""

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_stored_row_is_ignored_no_attribute_reappears(self) -> None:
        ConfigSetting.objects.set_value("worktrees_dir", "/srv/wt")
        settings = get_effective_settings()
        assert not hasattr(settings, "worktrees_dir")


class TestOnBehalfGatingStillResolves(TestCase):
    """Removing the dead field/shim leaves on-behalf gating fully intact."""

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_ON_BEHALF_AUTO_ACTIONS"):
            monkeypatch.delenv(env, raising=False)

    def test_a_forbidding_posture_blocks_visible_posts_and_drafts_auto(self) -> None:
        assert resolve_on_behalf_verdict("post_comment", egress_forbidden=True) is OnBehalfVerdict.BLOCK
        assert resolve_on_behalf_verdict("post_draft_note", egress_forbidden=True) is OnBehalfVerdict.AUTO_DRAFT

    def test_a_permitting_posture_resolves_to_proceed(self) -> None:
        assert resolve_on_behalf_verdict("post_comment", egress_forbidden=False) is OnBehalfVerdict.PROCEED


#: The seven loop-scanner flags retired as shipped-but-disabled. Each shipped ``False`` and was
#: never flipped, so the feature it named had never once run; the behaviour is unconditional
#: now and no setting, stored row or env var can turn it off again.
RETIRED_LOOP_SCANNER_FLAGS: tuple[str, ...] = (
    "auto_disposition_enabled",
    "auto_update_reinstall",
    "gitlab_approval_scanner_enabled",
    "mr_conflict_scan_enabled",
    "mr_triage_enabled",
    "review_nag_enabled",
    "review_resume_reply_enabled",
)


class TestLoopScannerFlagsRemoved:
    """None of the seven is a field, a parser entry, or a shipped default any more."""

    @pytest.mark.parametrize("key", RETIRED_LOOP_SCANNER_FLAGS)
    def test_key_is_not_a_user_settings_field(self, key: str) -> None:
        assert key not in _field_names()

    @pytest.mark.parametrize("key", RETIRED_LOOP_SCANNER_FLAGS)
    def test_key_is_not_in_the_overlay_overridable_registry(self, key: str) -> None:
        assert key not in OVERLAY_OVERRIDABLE_SETTINGS

    @pytest.mark.parametrize("key", RETIRED_LOOP_SCANNER_FLAGS)
    def test_key_is_recorded_as_retired(self, key: str) -> None:
        assert key in {retired.key for retired in RETIRED_SETTINGS}

    def test_no_src_module_reads_any_of_them(self) -> None:
        offenders: list[str] = []
        for path in _SRC.rglob("*.py"):
            if path.name in _LEDGER_MODULES or "migrations" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for lineno, line in enumerate(text.splitlines(), start=1):
                hit = next((key for key in RETIRED_LOOP_SCANNER_FLAGS if key in line), None)
                if hit is not None:
                    offenders.append(f"{path.relative_to(_REPO_ROOT).as_posix()}:{lineno}: {line.strip()}")
        assert offenders == [], (
            "A retired loop-scanner flag is still named in src/ — the behaviour is "
            "unconditional, so there is nothing left to read:\n" + "\n".join(offenders)
        )

    def test_the_env_override_for_the_reinstall_queue_is_gone(self) -> None:
        assert "T3_LOOP_AUTO_UPDATE" not in ENV_SETTING_OVERRIDES

    def test_the_reinstall_retirement_claims_no_guarantee_the_bypass_breaks(self) -> None:
        # `auto_update_require_green_main` is operator-settable, so "no pull happens on
        # a non-green default branch" holds only under its default. An unpinned prose
        # claim drifts back, and this one justified deleting the flag.
        reason = next(r.reason for r in RETIRED_SETTINGS if r.key == "auto_update_reinstall")
        assert "so no pull happens on a non-green default branch" not in reason


class TestStoredLoopScannerFlagRowsResolveToNothing(TestCase):
    """A stored row for any of the seven supplies no value — the keys are off the registry.

    This is the half that matters on a live box: one of these had a real overlay-scoped
    `ConfigSetting` row, so the deletion has to be proven against a stored row rather
    than only against a fresh dataclass.
    """

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_a_stored_row_reappears_on_no_resolved_attribute(self) -> None:
        for key in RETIRED_LOOP_SCANNER_FLAGS:
            ConfigSetting.objects.set_value(key, value=True)
        settings = get_effective_settings()
        assert [key for key in RETIRED_LOOP_SCANNER_FLAGS if hasattr(settings, key)] == []

    def test_an_overlay_scoped_row_cannot_turn_the_behaviour_off(self) -> None:
        ConfigSetting.objects.set_value("review_nag_enabled", value=False, scope="t3-acme")
        assert not hasattr(get_effective_settings("t3-acme"), "review_nag_enabled")
