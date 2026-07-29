# test-path: cross-cutting — asserts a skills/e2e/SKILL.md doc invariant; the
# browser_diagnosis import is only the shared flag constant, not the unit under test.
"""e2e/SKILL.md must document chrome-devtools-mcp as the default browser tool.

chrome-devtools-mcp replaced the Claude-in-Chrome extension as teatree's browser
tool: it drives its own Chrome over CDP with no claude.ai account and no
extension pairing, so the old account-switch / extension-popup fragility is gone.
The e2e skill must carry this so an agent reading it before a browser run knows
the tool, the registration path, and how to pre-authorize it for an unattended
run — while deterministic E2E stays on Playwright.

Doc-invariant guard in the spirit of ``test_ship_skill_documents_skip_flags``.
Per ``/t3:code`` § 5d the relationship assertions scan every occurrence of the
anchor token rather than keying on the first match.
"""

from pathlib import Path

from teatree.core.evidence.browser_diagnosis import CHROME_DEVTOOLS_HEADLESS_FLAG

_E2E_SKILL = Path(__file__).resolve().parents[1] / "skills" / "e2e" / "SKILL.md"


def _any_window_contains(text: str, anchor: str, *, must_include: str, radius: int) -> bool:
    start = 0
    while (idx := text.find(anchor, start)) != -1:
        window = text[max(0, idx - radius) : idx + len(anchor) + radius]
        if must_include in window:
            return True
        start = idx + 1
    return False


class TestE2ESkillDocumentsBrowserTool:
    def test_has_browser_tool_subsection(self) -> None:
        text = _E2E_SKILL.read_text(encoding="utf-8")
        assert "Browser tool: chrome-devtools-mcp" in text, (
            "e2e/SKILL.md must carry a 'Browser tool: chrome-devtools-mcp' subsection "
            "naming chrome-devtools-mcp as teatree's default browser tool."
        )

    def test_names_default_browser_tool(self) -> None:
        text = _E2E_SKILL.read_text(encoding="utf-8")
        assert _any_window_contains(
            text,
            "chrome-devtools-mcp",
            must_include="default browser tool",
            radius=200,
        ), "e2e/SKILL.md must state chrome-devtools-mcp is the default browser tool."

    def test_needs_no_account_or_extension_pairing(self) -> None:
        text = _E2E_SKILL.read_text(encoding="utf-8")
        assert _any_window_contains(
            text,
            "no claude.ai account",
            must_include="extension pairing",
            radius=120,
        ), (
            "e2e/SKILL.md must state chrome-devtools-mcp needs no claude.ai account and "
            "no browser-extension pairing (the account-switch / extension fragility is gone)."
        )

    def test_documents_registration_command(self) -> None:
        text = _E2E_SKILL.read_text(encoding="utf-8")
        assert "t3 mcp browser-diagnosis" in text, (
            "e2e/SKILL.md must name `t3 mcp browser-diagnosis` as the registration path."
        )
        assert "claude mcp add chrome-devtools" in text, (
            "e2e/SKILL.md must show the `claude mcp add chrome-devtools` registration line."
        )

    def test_registration_line_is_headless(self) -> None:
        text = _E2E_SKILL.read_text(encoding="utf-8")
        assert f"chrome-devtools-mcp@latest {CHROME_DEVTOOLS_HEADLESS_FLAG}" in text, (
            "e2e/SKILL.md's registration line must carry the headless flag — upstream defaults it "
            "to false, so omitting it opens a visible Chrome window on the user's desktop."
        )
        assert _any_window_contains(
            text,
            "--headless",
            must_include="never headed",
            radius=600,
        ), "e2e/SKILL.md must state teatree always runs the browser headless, never headed."

    def test_documents_unattended_allow_rule(self) -> None:
        text = _E2E_SKILL.read_text(encoding="utf-8")
        assert "mcp__chrome-devtools__*" in text, (
            "e2e/SKILL.md must give the `mcp__chrome-devtools__*` allow-rule to pre-authorize "
            "the tool for an unattended run."
        )

    def test_documents_mcp_specifier_has_no_domain_argument(self) -> None:
        """The research finding must be recorded: MCP specifiers take no argument pattern."""
        text = _E2E_SKILL.read_text(encoding="utf-8")
        assert _any_window_contains(
            text,
            "mcp__chrome-devtools",
            must_include="domain",
            radius=600,
        ), (
            "e2e/SKILL.md must show the MCP allow-rule form for the browser tool and note "
            "MCP specifiers cannot constrain by domain (no wildcard subdomains)."
        )

    def test_deterministic_e2e_stays_on_playwright(self) -> None:
        text = _E2E_SKILL.read_text(encoding="utf-8")
        assert _any_window_contains(
            text,
            "Playwright",
            must_include="chrome-devtools-mcp",
            radius=300,
        ), (
            "e2e/SKILL.md must keep deterministic E2E on Playwright, with chrome-devtools-mcp "
            "as the agentic nav/interaction + diagnosis lane, never the enforcement lane."
        )
