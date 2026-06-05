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

    def test_overlay_can_override_require_review_context(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Deep-retrieval gate is per-overlay overridable.

        A spec-heavy overlay can require deep retrieval before any review
        verdict while the global default stays off — runs through the generic
        ``OVERLAY_OVERRIDABLE_SETTINGS`` registry.
        """
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "specheavy")

        _write_toml(
            config_file,
            """
[teatree]
require_review_context = false

[overlays.specheavy]
class = "x.y:Z"
require_review_context = true
""",
        )

        assert get_effective_settings().require_review_context is True

    def test_overlay_can_override_orchestrator_bash_gate_enabled(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#115: per-overlay kill-switch for the orchestrator-Bash gate.

        An overlay can disable the heavy-Bash boundary gate while the
        global default stays on. Runs through the generic
        ``OVERLAY_OVERRIDABLE_SETTINGS`` registry.
        """
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "looseshell")

        _write_toml(
            config_file,
            """
[teatree]
orchestrator_bash_gate_enabled = true

[overlays.looseshell]
class = "x.y:Z"
orchestrator_bash_gate_enabled = false
""",
        )

        assert get_effective_settings().orchestrator_bash_gate_enabled is False

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

    def test_overlay_can_override_issue_implementer_settings(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1548: issue-implementer knobs are per-overlay overridable.

        A trusted overlay can enable the loop and raise its concurrency
        cap while the global default stays OFF. Runs through the generic
        ``OVERLAY_OVERRIDABLE_SETTINGS`` registry.
        """
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.delenv("T3_ISSUE_IMPLEMENTER_ENABLED", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")

        _write_toml(
            config_file,
            """
[teatree]
issue_implementer_enabled = false
issue_implementer_max_concurrent = 1

[overlays.trusted]
class = "x.y:Z"
issue_implementer_enabled = true
issue_implementer_label = "auto-implement"
issue_implementer_max_concurrent = 3
""",
        )

        effective = get_effective_settings()
        assert effective.issue_implementer_enabled is True
        assert effective.issue_implementer_label == "auto-implement"
        assert effective.issue_implementer_max_concurrent == 3

    def test_env_kill_switch_beats_overlay_override(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1548: the env kill-switch wins over a per-overlay enable.

        Operational fast-disable must beat an overlay that opted the loop
        on, mirroring ``T3_MODE`` beating the overlay ``mode`` override.
        """
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_ISSUE_IMPLEMENTER_ENABLED", "false")
        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")

        _write_toml(
            config_file,
            """
[teatree]
issue_implementer_enabled = false

[overlays.trusted]
class = "x.y:Z"
issue_implementer_enabled = true
""",
        )

        assert get_effective_settings().issue_implementer_enabled is False

    def test_overlay_can_override_mr_title_regex(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1540: per-overlay MR title pattern wins over the global.

        An overlay whose title grammar differs declares its own
        ``mr_title_regex`` without flipping the global default. The
        ``pr create`` gate reads it via ``get_effective_settings()`` so the
        per-overlay value applies when ``T3_OVERLAY_NAME`` is set.
        """
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "scoped")

        _write_toml(
            config_file,
            r"""
[teatree]
mr_title_regex = "^(feat|fix): .+"

[overlays.scoped]
class = "x.y:Z"
mr_title_regex = "^JIRA-\\d+: .+"
""",
        )

        assert get_effective_settings().mr_title_regex == r"^JIRA-\d+: .+"

    def test_e2e_mandatory_gate_default_on_and_overlay_can_disable(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1967: the mandatory-E2E gate defaults ON and is per-overlay disablable.

        Its OWN kill-switch ``e2e_mandatory_gate_enabled`` (never a reuse of
        another gate's switch) defaults to ``True`` and an overlay can disable
        it via ``[overlays.<name>]`` without flipping the global.
        """
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_MODE", raising=False)

        _write_toml(config_file, "[teatree]\n")
        assert get_effective_settings().e2e_mandatory_gate_enabled is True

        monkeypatch.setenv("T3_OVERLAY_NAME", "scoped")
        _write_toml(
            config_file,
            """
[teatree]

[overlays.scoped]
class = "x.y:Z"
e2e_mandatory_gate_enabled = false
""",
        )
        assert get_effective_settings().e2e_mandatory_gate_enabled is False
