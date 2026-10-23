"""Cold hook defaults come from declarations, including when the store is empty."""

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from hooks.scripts import teatree_settings
from teatree.config import cold_reader
from teatree.config.registries import COLD_HOOK_SETTINGS, tier_models

ROOT = Path(__file__).resolve().parents[2]
TYPED_READERS = {
    "teatree_bool_setting",
    "teatree_int_setting",
    "section_bool_setting",
    "section_int_setting",
    "bool_setting",
    "int_setting",
    "str_setting",
    "list_setting",
}


def default_keyword_sites(source: str) -> list[int]:
    tree = ast.parse(source)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id.lstrip("_") in TYPED_READERS)
            or (isinstance(node.func, ast.Attribute) and node.func.attr in TYPED_READERS)
        )
        and any(keyword.arg == "default" for keyword in node.keywords)
    ]


def test_the_scanner_flags_a_planted_literal() -> None:
    assert default_keyword_sites('teatree_bool_setting("x", default=True)') == [1]


def test_the_scanner_flags_a_planted_dynamic_default() -> None:
    assert default_keyword_sites('teatree_bool_setting("x", default=chosen)') == [1]


def test_no_cold_read_site_passes_a_default_keyword() -> None:
    sites = {
        str(path.relative_to(ROOT)): default_keyword_sites(path.read_text())
        for base in (ROOT / "hooks", ROOT / "src")
        for path in base.rglob("*.py")
    }
    assert {path: lines for path, lines in sites.items() if lines} == {}


def test_a_gate_follows_its_registry_default_when_no_row_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(teatree_settings, "_cold_db_bool", lambda _name: None)
    key = "banned_terms_gate_enabled"
    assert teatree_settings.teatree_bool_setting(key) is True
    monkeypatch.setitem(COLD_HOOK_SETTINGS, key, replace(COLD_HOOK_SETTINGS[key], default=False))
    assert teatree_settings.teatree_bool_setting(key) is False


def test_cold_reader_uses_the_declared_default_without_a_caller_literal(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite3"
    assert cold_reader.bool_setting("git_add_all_gate_enabled", db_path=missing) is True
    assert cold_reader.bool_setting("danger_gate_fail_open", db_path=missing) is False
    assert cold_reader.int_setting("hook_validator_timeout_seconds", db_path=missing) == 60
    assert cold_reader.str_setting("slack_user_id", db_path=missing) == ""
    assert cold_reader.list_setting("private_repos", db_path=missing) == []


def test_cold_reader_does_not_expose_mutable_registry_defaults(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite3"
    value = cold_reader.list_setting("private_repos", db_path=missing)
    value.append("changed")

    assert cold_reader.list_setting("private_repos", db_path=missing) == []


def test_registry_model_maps_are_independent_copies() -> None:
    models = tier_models()
    models["frontier"] = "changed"

    assert tier_models()["frontier"] != "changed"


def test_autoload_follows_a_changed_declaration_when_the_store_has_no_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("T3_AUTOLOAD", raising=False)
    monkeypatch.setattr(teatree_settings, "_cold_db_bool", lambda _name: None)
    monkeypatch.setattr(teatree_settings, "_declared_default", lambda _name: True)

    assert teatree_settings.autoload_enabled() is True


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("orchestrator_turn_budget", 25),
        ("orchestrator_turn_wall_clock_seconds", 180),
        ("hook_validator_timeout_seconds", 60),
    ],
)
def test_hook_budget_has_a_safe_emergency_value_when_the_declaration_cannot_load(
    key: str, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(teatree_settings, "_declared_default", lambda _name: None)
    monkeypatch.setattr(teatree_settings, "_cold_db_int", lambda _name: None)

    assert teatree_settings.teatree_int_setting(key) == expected


def test_cold_shell_output_stays_bounded_when_the_declaration_cannot_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unavailable(_name: str) -> object:
        raise ModuleNotFoundError

    monkeypatch.setattr(cold_reader, "import_module", unavailable)
    assert cold_reader.int_setting("agent_shell_max_output_bytes", db_path=tmp_path / "missing") == 16384


def test_an_unimportable_registry_keeps_a_gate_armed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def unavailable(_name: str) -> object:
        raise ModuleNotFoundError

    monkeypatch.setattr(cold_reader, "import_module", unavailable)
    assert cold_reader.bool_setting("git_add_all_gate_enabled", db_path=tmp_path / "missing.sqlite3") is True
