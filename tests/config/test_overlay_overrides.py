"""Per-overlay override machinery.

Split verbatim from the former monolithic ``tests/test_config.py``
(souliane/teatree#443). Covers the ``OVERLAY_OVERRIDABLE_SETTINGS``
resolution chain (env → active overlay override → global → dataclass
default), the active-overlay selection via ``T3_OVERLAY_NAME``, and
per-overlay mode parsing/validation.

Integration-first per the Test-Writing Doctrine: real TOML fixtures
under ``tmp_path`` with ``teatree.config.CONFIG_PATH`` monkeypatched.
"""

from pathlib import Path

import pytest

from teatree.config import Mode, discover_overlays, get_effective_settings

from ._shared import _write_toml


class TestOverlayOverrides:
    """Per-overlay overrides for any key in ``OVERLAY_OVERRIDABLE_SETTINGS``.

    The resolution chain is env (where applicable) → active overlay override
    → global → dataclass default. The active overlay is picked via
    ``T3_OVERLAY_NAME`` when set, else cwd-based discovery.
    """

    def test_overlay_toml_mode_parsed(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(
            config_path,
            """
[teatree]
mode = "interactive"

[overlays.my-overlay]
class = "x.y:Z"
mode = "auto"
""",
        )
        entries = discover_overlays(config_path=config_path)
        by_name = {e.name: e for e in entries}
        assert by_name["my-overlay"].overrides["mode"] is Mode.AUTO

    def test_overlay_invalid_mode_raises(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(
            config_path,
            """
[overlays.my-overlay]
class = "x.y:Z"
mode = "nope"
""",
        )
        with pytest.raises(ValueError, match="Invalid t3 mode"):
            discover_overlays(config_path=config_path)

    def test_overlay_override_wins_over_global(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")

        _write_toml(
            config_file,
            """
[teatree]
mode = "interactive"
branch_prefix = "ac"

[overlays.my-overlay]
class = "x.y:Z"
mode = "auto"
branch_prefix = "xp"
""",
        )

        effective = get_effective_settings()
        assert effective.mode is Mode.AUTO
        assert effective.branch_prefix == "xp"

    def test_overlay_without_override_inherits_global(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "x")

        _write_toml(
            config_file,
            """
[teatree]
mode = "auto"

[overlays.x]
class = "x.y:Z"
""",
        )

        assert get_effective_settings().mode is Mode.AUTO

    def test_env_var_beats_overlay_override(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_MODE", "interactive")
        monkeypatch.setenv("T3_OVERLAY_NAME", "x")

        _write_toml(
            config_file,
            """
[teatree]
mode = "interactive"

[overlays.x]
class = "x.y:Z"
mode = "auto"
""",
        )

        assert get_effective_settings().mode is Mode.INTERACTIVE

    def test_t3_overlay_name_selects_entry(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "b")

        _write_toml(
            config_file,
            """
[teatree]
mode = "interactive"

[overlays.a]
class = "x"
mode = "interactive"

[overlays.b]
class = "y"
mode = "auto"
""",
        )

        assert get_effective_settings().mode is Mode.AUTO

    def test_overlay_can_override_user_identity_aliases(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A per-overlay alias list wins over the global setting.

        Different platforms may use different handle conventions, so an
        overlay scoped to one tracker can carry a tracker-specific alias
        list without flipping the global default.
        """
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "scoped")

        _write_toml(
            config_file,
            """
[teatree]
user_identity_aliases = ["souliane"]

[overlays.scoped]
class = "x.y:Z"
user_identity_aliases = ["adrien.work", "souliane", "adrien.cossa"]
""",
        )

        assert get_effective_settings().user_identity_aliases == ["adrien.work", "souliane", "adrien.cossa"]

    def test_overlay_can_override_require_human_approval_to_answer(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Answerer-autonomy is per-overlay overridable.

        It flows through the generic ``OVERLAY_OVERRIDABLE_SETTINGS``
        registry — a trusted overlay can opt into direct posting without
        flipping the global.
        """
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")

        _write_toml(
            config_file,
            """
[teatree]
require_human_approval_to_answer = true

[overlays.trusted]
class = "x.y:Z"
require_human_approval_to_answer = false
""",
        )

        assert get_effective_settings().require_human_approval_to_answer is False

    def test_overlay_can_override_notify_user_via_bot(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bot→user notification toggle is per-overlay overridable (#963).

        A noisy overlay can opt out of the bot DM channel while leaving the
        global default on — runs through the same generic
        ``OVERLAY_OVERRIDABLE_SETTINGS`` registry.
        """
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "quiet")

        _write_toml(
            config_file,
            """
[teatree]
notify_user_via_bot = true

[overlays.quiet]
class = "x.y:Z"
notify_user_via_bot = false
""",
        )

        assert get_effective_settings().notify_user_via_bot is False

    def test_overlay_can_override_notify_on_post_on_behalf(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After-receipt DM toggle is per-overlay overridable (#949).

        SKILL.md:277 requires an independent per-overlay notify lifetime —
        an overlay can flip the after-receipt DM off while the global
        default stays on. Runs through the generic
        ``OVERLAY_OVERRIDABLE_SETTINGS`` registry.
        """
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "foo")

        _write_toml(
            config_file,
            """
[teatree]
notify_on_post_on_behalf = true

[overlays.foo]
class = "x.y:Z"
notify_on_post_on_behalf = false
""",
        )

        assert get_effective_settings().notify_on_post_on_behalf is False

    def test_overlay_can_override_max_concurrent_local_stacks(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1397: per-overlay cap on concurrent local stacks.

        A heavy overlay caps to ``1`` while the global default stays
        unbounded (``0``). Runs through the generic
        ``OVERLAY_OVERRIDABLE_SETTINGS`` registry so the gate (which
        reads ``get_effective_settings().max_concurrent_local_stacks``)
        picks up the per-overlay value when ``T3_OVERLAY_NAME`` is set.
        """
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "heavy")

        _write_toml(
            config_file,
            """
[teatree]
max_concurrent_local_stacks = 0

[overlays.heavy]
class = "x.y:Z"
max_concurrent_local_stacks = 1
""",
        )

        assert get_effective_settings().max_concurrent_local_stacks == 1
