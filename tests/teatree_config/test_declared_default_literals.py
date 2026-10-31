"""A reader never restates a declared default as a literal beside the key it reads."""

import ast
from collections.abc import Mapping
from pathlib import Path

from teatree.config.declared_defaults import declared_defaults

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "teatree"

#: Smaller integers collide with unrelated constants too often to mean "this is the default".
MIN_LITERAL = 20

#: Where a declaration legitimately states its own default.
DECLARATION_FILES = ("settings", "registries", "declared_defaults", "declaration")

ALLOWED: Mapping[tuple[str, str, int], str] = {
    (
        "dash/pr_board.py",
        "review_nag_max_interval_days",
        30,
    ): "minutes before a board inventory is stale; equal to the declared interval in days by coincidence",
}


def named_literal_defaults(source: str, declared: Mapping[str, object]) -> list[tuple[str, int, int]]:
    """``(key, literal, line)`` for each integer equal to the declared default of a key *source* names."""
    tree = ast.parse(source)
    names = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    named = {
        key: value for key, value in declared.items() if key in names and type(value) is int and value >= MIN_LITERAL
    }
    return [
        (key, node.value, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and type(node.value) is int
        for key, value in named.items()
        if node.value == value
    ]


def is_declaration_file(path: Path) -> bool:
    relative = path.relative_to(SOURCE_ROOT)
    return (relative.parts[0] == "config" and path.stem.startswith(DECLARATION_FILES)) or "migrations" in relative.parts


def test_the_scan_flags_a_literal_default_beside_the_key_it_restates() -> None:
    source = 'KEY = "loop_cadence_seconds"\n\n\ndef fallback() -> int:\n    return 720\n'

    assert named_literal_defaults(source, {"loop_cadence_seconds": 720}) == [("loop_cadence_seconds", 720, 5)]


def test_the_scan_flags_a_literal_beside_an_attribute_read_of_the_key() -> None:
    source = "def wait(settings):\n    return settings.loop_cadence_seconds or 720\n"

    assert named_literal_defaults(source, {"loop_cadence_seconds": 720}) == [("loop_cadence_seconds", 720, 2)]


def test_the_scan_ignores_the_same_literal_in_a_module_that_does_not_name_the_key() -> None:
    assert named_literal_defaults("def retry() -> int:\n    return 720\n", {"loop_cadence_seconds": 720}) == []


def test_the_scan_ignores_literals_below_the_noise_cut_and_non_integer_defaults() -> None:
    source = 'KEYS = ("small", "flag")\nLIMIT = 3\nFLAG = True\n'

    assert named_literal_defaults(source, {"small": 3, "flag": True}) == []


def restated_defaults() -> set[tuple[str, str, int]]:
    declared = declared_defaults()
    return {
        (path.relative_to(SOURCE_ROOT).as_posix(), key, literal)
        for path in SOURCE_ROOT.rglob("*.py")
        if not is_declaration_file(path)
        for key, literal, _line in named_literal_defaults(path.read_text(encoding="utf-8"), declared)
    }


def test_no_reader_restates_a_declared_default_as_a_literal() -> None:
    assert restated_defaults() - set(ALLOWED) == set()


def test_every_allowlisted_literal_is_still_present() -> None:
    assert set(ALLOWED) <= restated_defaults()
