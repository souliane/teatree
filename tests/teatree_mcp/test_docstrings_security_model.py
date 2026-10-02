"""Doc-invariant tests for the MCP security model (must-fix §2).

The shipped MCP surface is read tools + gate-preserving write tools, NOT
read-only. These pin that the module docstrings, the server instructions, and the
setup skill no longer claim the surface is read-only-only (which gave agents a
wrong mental model of what is reachable and gated over ``mcp__teatree__*``).
"""

from pathlib import Path

import teatree.mcp
import teatree.mcp.server
from teatree.mcp import build_server


def _collapsed(text: str) -> str:
    r"""Lower-case *text* with all runs of whitespace collapsed to single spaces.

    Docstrings wrap phrases like "write\ntools" across lines, so a substring
    check must be whitespace-insensitive to survive reflowing.
    """
    return " ".join(text.lower().split())


_REPO_ROOT = Path(__file__).parents[2]
_SKILL_FILES = (
    _REPO_ROOT / "skills" / "setup" / "SKILL.md",
    _REPO_ROOT / "skills" / "setup" / "references" / "agent-mode-and-mcp-config.md",
)


class TestModuleDocstrings:
    def test_server_docstring_does_not_claim_read_only_only(self) -> None:
        doc = teatree.mcp.server.__doc__ or ""
        assert "read-only tools only" not in doc
        assert "write tools" in _collapsed(doc)

    def test_package_docstring_does_not_claim_read_only(self) -> None:
        doc = teatree.mcp.__doc__ or ""
        assert "Read-only: mutations stay" not in doc
        assert "write tools" in _collapsed(doc)


class TestServerInstructions:
    def test_instructions_do_not_advertise_a_read_only_only_surface(self) -> None:
        instructions = build_server().instructions or ""
        assert "All tools are read-only" not in instructions
        assert "gate-preserving" in instructions.lower()
        # The gate-preserving write section is advertised.
        assert "Teatree write tools" in instructions


class TestSetupSkillInventory:
    def test_skill_files_do_not_claim_five_read_only_tools(self) -> None:
        for path in _SKILL_FILES:
            text = path.read_text(encoding="utf-8")
            assert "five read-only" not in text, f"{path} still enumerates 'five read-only tools'"
            assert "gate-preserving" in text, f"{path} does not describe the gate-preserving write surface"
