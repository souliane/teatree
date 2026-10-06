# test-path: cross-cutting — asserts a skills/e2e doc invariant against the `t3 browser` CLI it documents.
"""The e2e skill documents `t3 browser` as the browser tool, and it matches the CLI.

The skill's documentation is its SKILL.md spine PLUS the ``references/`` files the
spine points at, so each invariant holds when ANY of those documents carries it.
Per ``/t3:code`` § 5d the relationship assertions scan every occurrence of the
anchor token rather than keying on the first match.
"""

import re
from pathlib import Path

from teatree.browser.session import Verb
from teatree.cli.browser import browser_app

_E2E_SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "e2e"
_E2E_DOCS: tuple[str, ...] = tuple(
    path.read_text(encoding="utf-8")
    for path in (_E2E_SKILL_DIR / "SKILL.md", *sorted((_E2E_SKILL_DIR / "references").glob("*.md")))
)


def _any_doc_contains(needle: str) -> bool:
    return any(needle in text for text in _E2E_DOCS)


def _any_window_contains(text: str, anchor: str, *, must_include: str, radius: int) -> bool:
    start = 0
    while (idx := text.find(anchor, start)) != -1:
        window = text[max(0, idx - radius) : idx + len(anchor) + radius]
        if must_include in window:
            return True
        start = idx + 1
    return False


def _any_doc_window_contains(anchor: str, *, must_include: str, radius: int) -> bool:
    return any(_any_window_contains(text, anchor, must_include=must_include, radius=radius) for text in _E2E_DOCS)


def test_has_the_browser_tool_section() -> None:
    assert _any_doc_contains("## Browser tool: `t3 browser` (Playwright, headless)")


def test_documents_every_browser_command_the_cli_has() -> None:
    commands = sorted(command.name or command.callback.__name__ for command in browser_app.registered_commands)

    assert commands == ["act", "close", "inspect", "open"]
    assert [name for name in commands if not _any_doc_contains(f"`t3 browser {name}")] == []


def test_documents_every_act_verb() -> None:
    assert [verb for verb in Verb if not _any_doc_contains(f"| `{verb}` |")] == []


def test_states_the_browser_always_runs_headless() -> None:
    assert _any_doc_window_contains("headless", must_include="never headed", radius=200)
    assert _any_doc_contains("has no headed mode")


def test_deterministic_e2e_stays_on_the_e2e_runner() -> None:
    assert _any_doc_window_contains("`t3 browser`", must_include="never the enforcement lane", radius=400)


def test_names_no_mcp_server_but_teatrees() -> None:
    foreign = re.compile(r"mcp__(?!teatree__|plugin_t3_teatree__)\w+")

    assert [match for text in _E2E_DOCS for match in foreign.findall(text)] == []
    assert not _any_doc_contains("claude mcp add")
