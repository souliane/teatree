"""The one scan every published text crosses: the publish command and the merge gate share it."""

import re
from pathlib import Path

import pytest

from teatree.core.publishing.live_text import FindingKind, scan_live_text

_REPO = "souliane/teatree"
_FOOTER = "\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)"
_OWN_ISSUE = "souliane/teatree#12"
_CORE = Path(__file__).resolve().parents[3] / "src" / "teatree" / "core"


def _kinds(text: str, allowed: frozenset[str] = frozenset()) -> set[FindingKind]:
    return {finding.kind for finding in scan_live_text(text, _REPO, "github", allowed).findings}


class TestClosingKeywords:
    @pytest.mark.parametrize(
        "text",
        [
            "Closes #12",
            "fixes: #12",
            "Resolved souliane/teatree#12",
            "closes https://github.com/souliane/teatree/issues/12",
        ],
    )
    def test_a_closing_keyword_is_a_finding(self, text: str) -> None:
        assert FindingKind.CLOSING_KEYWORD in _kinds(text)

    @pytest.mark.parametrize(
        "text",
        ["Closes #12", "Fixes souliane/teatree#12", "Resolves https://github.com/souliane/teatree/issues/12"],
    )
    def test_the_allowed_ref_passes_in_every_spelling(self, text: str) -> None:
        assert _kinds(text, frozenset({_OWN_ISSUE})) == set()

    def test_another_ref_is_still_a_finding_when_one_is_allowed(self) -> None:
        assert _kinds("Closes #13", frozenset({_OWN_ISSUE})) == {FindingKind.CLOSING_KEYWORD}

    def test_a_ref_in_another_repo_is_not_the_allowed_one(self) -> None:
        assert _kinds("Closes other/repo#12", frozenset({_OWN_ISSUE})) == {FindingKind.CLOSING_KEYWORD}

    def test_a_relates_to_line_is_clean(self) -> None:
        assert _kinds("Relates to #12") == set()


class TestAiSignature:
    def test_a_robot_footer_is_a_finding(self) -> None:
        assert FindingKind.AI_SIGNATURE in _kinds(f"Tidies it.\n\n{_FOOTER}")

    def test_a_footer_described_in_prose_is_clean(self) -> None:
        assert _kinds(f"Refuses a body ending in `{_FOOTER}`.") == set()


@pytest.mark.real_merge_privacy_scan
@pytest.mark.usefixtures("configured_banned_term_registry")
class TestPublicTargetScans:
    def test_a_private_ip_to_a_public_repo_is_a_leak(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.core.gates.privacy_gate._target_is_public", lambda *_, **__: True)
        verdict = scan_live_text("Connect to 10.24.8.9", _REPO, "github")  # privacy-scan:allow synthetic fixture
        assert FindingKind.LEAK in {finding.kind for finding in verdict.findings}
        assert "generic:private_ip" in verdict.render()

    def test_a_structured_user_quote_to_a_public_repo_is_a_quote_finding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.core.gates.privacy_gate._target_is_public", lambda *_, **__: True)
        verdict = scan_live_text('> "Never merge on a Friday," the owner wrote.', _REPO, "github")
        assert {finding.kind for finding in verdict.findings} == {FindingKind.USER_QUOTE}

    def test_the_same_text_to_a_private_repo_is_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.core.gates.privacy_gate._target_is_public", lambda *_, **__: False)
        assert scan_live_text("Connect to 10.24.8.9", _REPO, "github").clean  # privacy-scan:allow synthetic fixture


def test_the_close_keyword_pattern_is_defined_in_exactly_one_core_module() -> None:
    definitions = [
        path.relative_to(_CORE).as_posix()
        for path in _CORE.rglob("*.py")
        if re.search(r"^CLOSE_KEYWORD_RE\s*=", path.read_text(encoding="utf-8"), re.MULTILINE)
    ]
    assert definitions == ["publishing/close_keyword.py"]
