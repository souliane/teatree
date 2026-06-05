"""e2e/SKILL.md must document the Claude-in-Chrome connectivity checklist.

A real account switch (souliane/teatree#1916) surfaced that being logged
into claude.ai in the browser does NOT mean the Claude-in-Chrome extension
is connected: `/mcp` shows the server reconnected while every browser tool
returns "extension not connected". The decisive diagnostic is
`list_connected_browsers` returning `[]`, and navigation can silently block
on per-origin permission prompts. The e2e skill must carry this so an agent
reading it before a browser run knows the probe, the fix, and that an MCP
server being connected does not imply the extension is paired.

Doc-invariant guard in the spirit of ``test_ship_skill_documents_skip_flags``.
Per ``/t3:code`` § 5d the relationship assertions scan every occurrence of
the anchor token rather than keying on the first match.
"""

from pathlib import Path

_E2E_SKILL = Path(__file__).resolve().parents[1] / "skills" / "e2e" / "SKILL.md"


def _any_window_contains(text: str, anchor: str, *, must_include: str, radius: int) -> bool:
    start = 0
    while (idx := text.find(anchor, start)) != -1:
        window = text[max(0, idx - radius) : idx + len(anchor) + radius]
        if must_include in window:
            return True
        start = idx + 1
    return False


class TestE2ESkillDocumentsChromeConnectivity:
    def test_has_connectivity_subsection(self) -> None:
        text = _E2E_SKILL.read_text(encoding="utf-8")
        assert "Claude in Chrome connectivity" in text, (
            "e2e/SKILL.md must carry a 'Claude in Chrome connectivity' subsection "
            "covering the extension-vs-claude.ai connection split (souliane/teatree#1916)."
        )

    def test_documents_list_connected_browsers_probe(self) -> None:
        text = _E2E_SKILL.read_text(encoding="utf-8")
        assert "list_connected_browsers" in text, (
            "e2e/SKILL.md must name the list_connected_browsers probe as the decisive extension-pairing diagnostic."
        )

    def test_empty_array_means_extension_not_paired(self) -> None:
        text = _E2E_SKILL.read_text(encoding="utf-8")
        assert _any_window_contains(
            text,
            "list_connected_browsers",
            must_include="empty array",
            radius=400,
        ), (
            "e2e/SKILL.md must state that an empty array from list_connected_browsers "
            "means the extension is not paired with the active account."
        )

    def test_logged_into_claude_ai_is_not_connected(self) -> None:
        text = _E2E_SKILL.read_text(encoding="utf-8")
        assert _any_window_contains(
            text,
            "claude.ai",
            must_include="extension",
            radius=400,
        ), (
            "e2e/SKILL.md must state that being logged into claude.ai does NOT mean "
            "the extension is connected (the popup has its own connection state)."
        )

    def test_documents_extension_popup_fix(self) -> None:
        text = _E2E_SKILL.read_text(encoding="utf-8")
        assert "popup" in text, "e2e/SKILL.md must give the fix path via the extension popup sign-in + Connect."
        assert "restart" in text.lower(), (
            "e2e/SKILL.md must mention a full browser restart when the popup fix is not enough."
        )

    def test_documents_per_origin_navigation_block(self) -> None:
        text = _E2E_SKILL.read_text(encoding="utf-8")
        assert "per-origin" in text, (
            "e2e/SKILL.md must warn that navigation can silently block on per-origin permission prompts."
        )

    def test_documents_mcp_specifier_has_no_domain_argument(self) -> None:
        """The research finding must be recorded: MCP specifiers take no argument pattern."""
        text = _E2E_SKILL.read_text(encoding="utf-8")
        assert _any_window_contains(
            text,
            "mcp__claude-in-chrome",
            must_include="navigate",
            radius=600,
        ), (
            "e2e/SKILL.md must show the MCP allow-rule form for the browser tool and "
            "note MCP specifiers cannot constrain by domain (no wildcard subdomains)."
        )
        assert "AskUserQuestion" in text, (
            "e2e/SKILL.md must note the AskUserQuestion fallback for automated runs that hit a per-origin prompt."
        )

    def test_diagnosis_one_liner(self) -> None:
        text = _E2E_SKILL.read_text(encoding="utf-8")
        assert _any_window_contains(
            text,
            "extension connected",
            must_include="MCP server connected",
            radius=200,
        ), "e2e/SKILL.md must carry the one-liner: MCP server connected != extension connected."
