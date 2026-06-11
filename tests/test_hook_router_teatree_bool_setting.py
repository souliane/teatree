"""Tests for the shared ``_teatree_bool_setting`` helper (#1694).

``hook_router`` carried ~10 near-identical ``[teatree] <flag>_enabled``
boolean toml readers (try / ``tomllib.load`` / ``except Exception`` ->
default). They are extracted to one ``_teatree_bool_setting(name, *, default)``
helper and every reader delegates to it.

Two behaviors the helper must preserve exactly. A fail-OPEN reader
(``default=True``) returns ``True`` on a missing/empty/broken config and on
any value except a bare boolean ``false`` (a quoted ``"false"`` does NOT
disable). A fail-CLOSED reader (``default=False``) returns ``False`` on a
missing/empty/broken config and on any value except a bare boolean ``true``
(a quoted ``"true"`` does NOT enable).

The delegation assertions are anti-vacuous: monkeypatching the helper
flips every reader's output, which can only happen if the reader routes
through the helper.
"""

from pathlib import Path

import pytest

import hooks.scripts.hook_router as router

# (reader function name, [teatree] key) — every [teatree]-table boolean flag reader.
_FAIL_OPEN_READERS: tuple[tuple[str, str], ...] = (
    ("_deny_circuit_breaker_enabled", "deny_circuit_breaker_enabled"),
    ("_loop_registration_gate_enabled", "loop_registration_gate_enabled"),
    ("_skill_loading_gate_enabled", "skill_loading_gate_enabled"),
    ("_plan_edit_gate_enabled", "plan_edit_gate_enabled"),
    ("_mcp_privacy_gate_enabled", "mcp_privacy_gate_enabled"),
    ("_self_dm_gate_enabled", "self_dm_gate_enabled"),
    ("_orchestrator_bash_gate_enabled", "orchestrator_bash_gate_enabled"),
    # #1733: flipped to default-ON (fail-open) once the Agent matcher was wired.
    ("_orchestrator_boundary_agent_gate_enabled", "orchestrator_boundary_agent_gate_enabled"),
)

_FAIL_CLOSED_READERS: tuple[tuple[str, str], ...] = (
    ("_dispatch_quote_gate_on_task_create_enabled", "dispatch_quote_gate_on_task_create_enabled"),
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Route ``Path.home()`` (hence the toml read) at an empty tmp dir."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _write_teatree(home_dir: Path, body: str) -> None:
    (home_dir / ".teatree.toml").write_text(f"[teatree]\n{body}", encoding="utf-8")


class TestTeatreeBoolSetting:
    def test_missing_config_returns_default(self, home: Path) -> None:
        assert router._teatree_bool_setting("any_flag", default=True) is True
        assert router._teatree_bool_setting("any_flag", default=False) is False

    def test_empty_teatree_table_returns_default(self, home: Path) -> None:
        _write_teatree(home, "")
        assert router._teatree_bool_setting("any_flag", default=True) is True
        assert router._teatree_bool_setting("any_flag", default=False) is False

    def test_broken_toml_returns_default(self, home: Path) -> None:
        (home / ".teatree.toml").write_text("this is = not = valid [[[", encoding="utf-8")
        assert router._teatree_bool_setting("any_flag", default=True) is True
        assert router._teatree_bool_setting("any_flag", default=False) is False

    def test_explicit_false_disables_a_default_true_flag(self, home: Path) -> None:
        _write_teatree(home, "any_flag = false\n")
        assert router._teatree_bool_setting("any_flag", default=True) is False

    def test_explicit_true_enables_a_default_false_flag(self, home: Path) -> None:
        _write_teatree(home, "any_flag = true\n")
        assert router._teatree_bool_setting("any_flag", default=False) is True

    def test_quoted_false_does_not_disable_a_default_true_flag(self, home: Path) -> None:
        # Documented invariant: only a bare boolean ``false`` disables.
        _write_teatree(home, 'any_flag = "false"\n')
        assert router._teatree_bool_setting("any_flag", default=True) is True

    def test_quoted_true_does_not_enable_a_default_false_flag(self, home: Path) -> None:
        _write_teatree(home, 'any_flag = "true"\n')
        assert router._teatree_bool_setting("any_flag", default=False) is False

    def test_explicit_true_keeps_a_default_true_flag_enabled(self, home: Path) -> None:
        _write_teatree(home, "any_flag = true\n")
        assert router._teatree_bool_setting("any_flag", default=True) is True

    def test_explicit_false_keeps_a_default_false_flag_disabled(self, home: Path) -> None:
        _write_teatree(home, "any_flag = false\n")
        assert router._teatree_bool_setting("any_flag", default=False) is False


class TestFailOpenReadersBehavior:
    @pytest.mark.parametrize(("reader", "key"), _FAIL_OPEN_READERS)
    def test_defaults_enabled_without_config(self, reader: str, key: str, home: Path) -> None:
        assert getattr(router, reader)() is True

    @pytest.mark.parametrize(("reader", "key"), _FAIL_OPEN_READERS)
    def test_bare_false_disables(self, reader: str, key: str, home: Path) -> None:
        _write_teatree(home, f"{key} = false\n")
        assert getattr(router, reader)() is False

    @pytest.mark.parametrize(("reader", "key"), _FAIL_OPEN_READERS)
    def test_quoted_false_does_not_disable(self, reader: str, key: str, home: Path) -> None:
        _write_teatree(home, f'{key} = "false"\n')
        assert getattr(router, reader)() is True


class TestFailClosedReadersBehavior:
    @pytest.mark.parametrize(("reader", "key"), _FAIL_CLOSED_READERS)
    def test_defaults_disabled_without_config(self, reader: str, key: str, home: Path) -> None:
        assert getattr(router, reader)() is False

    @pytest.mark.parametrize(("reader", "key"), _FAIL_CLOSED_READERS)
    def test_bare_true_enables(self, reader: str, key: str, home: Path) -> None:
        _write_teatree(home, f"{key} = true\n")
        assert getattr(router, reader)() is True

    @pytest.mark.parametrize(("reader", "key"), _FAIL_CLOSED_READERS)
    def test_quoted_true_does_not_enable(self, reader: str, key: str, home: Path) -> None:
        _write_teatree(home, f'{key} = "true"\n')
        assert getattr(router, reader)() is False


class TestReadersDelegateToHelper:
    """Patching the helper flips every reader's output.

    Anti-vacuous: that flip can only happen if the reader routes through
    ``_teatree_bool_setting``.
    """

    @pytest.mark.parametrize(("reader", "key"), _FAIL_OPEN_READERS)
    def test_fail_open_reader_routes_through_helper(
        self, reader: str, key: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, bool]] = []

        def _fake(name: str, *, default: bool = True) -> bool:
            seen.append((name, default))
            return not default

        monkeypatch.setattr(router, "_teatree_bool_setting", _fake)
        # A fail-open reader defaults True, so the fake (which returns ``not
        # default``) forces it False — observable only through delegation.
        assert getattr(router, reader)() is False
        assert (key, True) in seen

    @pytest.mark.parametrize(("reader", "key"), _FAIL_CLOSED_READERS)
    def test_fail_closed_reader_routes_through_helper(
        self, reader: str, key: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, bool]] = []

        def _fake(name: str, *, default: bool = True) -> bool:
            seen.append((name, default))
            return not default

        monkeypatch.setattr(router, "_teatree_bool_setting", _fake)
        # A fail-closed reader defaults False, so the fake forces it True.
        assert getattr(router, reader)() is True
        assert (key, False) in seen
