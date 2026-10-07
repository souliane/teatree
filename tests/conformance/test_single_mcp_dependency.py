"""teatree depends on one MCP server, its own (souliane/teatree#5116).

The retired browser server, the third-party connector layer and the reconnect command
leave no trace in the tracked tree; and outside the guards that refuse or detect calls
to other MCP servers, nothing names a tool on one.
"""

import re
from pathlib import Path

from teatree.utils.run import run_allowed_to_fail, run_checked

_ROOT = Path(__file__).resolve().parents[2]
# Bracketed spellings, so this file never matches its own pattern.
_RETIRED = r"chrome[-]devtools|claude[_]ai|claude[.]ai connector|mcp[ ]reconnect"
_FOREIGN_TOOL = re.compile(r"\bmcp__(?!teatree__|plugin_t3_teatree__)[A-Za-z0-9-]+(?:_[A-Za-z0-9-]+)*__")
# Guards name the servers whose calls they refuse or detect; tests drive them.
_GUARD_PATHS = ("hooks/", "src/teatree/hooks/", "src/teatree/eval/", "tests/")


def _retired_hits(repo: Path) -> list[str]:
    result = run_allowed_to_fail(
        ["git", "-C", str(repo), "grep", "-n", "-I", "-i", "-E", _RETIRED], expected_codes=(0, 1)
    )
    return result.stdout.splitlines()


def test_no_tracked_file_names_a_retired_mcp_surface() -> None:
    assert _retired_hits(_ROOT) == []


def test_the_scan_finds_a_planted_retired_term(tmp_path: Path) -> None:
    run_checked(["git", "init", "-q", "-b", "main", str(tmp_path)])
    (tmp_path / "notes.md").write_text("register chrome" + "-devtools for the browser\n", encoding="utf-8")
    run_checked(["git", "-C", str(tmp_path), "add", "notes.md"])

    assert _retired_hits(tmp_path) == ["notes.md:1:register chrome" + "-devtools for the browser"]


def test_only_guards_name_a_tool_on_another_mcp_server() -> None:
    tracked = run_checked(["git", "-C", str(_ROOT), "ls-files", "-z"]).stdout.split("\0")
    offenders = {
        path: sorted(set(_FOREIGN_TOOL.findall(text)))
        for path in tracked
        if path and not path.startswith(_GUARD_PATHS) and path != "dev/.test_durations"
        for text in [_read(_ROOT / path)]
        if _FOREIGN_TOOL.search(text)
    }

    assert offenders == {}


def test_the_foreign_tool_pattern_spares_teatrees_own_server() -> None:
    assert _FOREIGN_TOOL.findall("mcp__slack__send mcp__plugin_t3_teatree__slack_react mcp__teatree__ticket_get") == [
        "mcp__slack__"
    ]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
        return ""
