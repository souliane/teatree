r"""The UserPromptSubmit renderer surfaces SOFT companion suggestions (#53).

``companions`` are the soft counterpart to the hard ``requires`` -> ``suggestions``
edge: surfaced in the prompt-submit message as an optional, complementary
suggestion, but NEVER written to ``<session>.pending`` (so the PreToolUse
skill-loading gate never hard-blocks on them). Before this wiring the value was
computed by ``suggest_skills`` and dropped on the floor — the renderer read only
``suggestions``/``advisory``. These pin the surfacing both directly on the
extracted renderer and end-to-end through the live handler.
"""

import sys
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_user_prompt_submit
from hooks.scripts.skill_suggestion_render import companion_suggestion_line, render_skill_suggestion_message

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import skill_loader as skill_loader_mod  # noqa: E402 — import follows the sys.path insert above


class TestCompanionSuggestionLine:
    def test_no_companions_renders_nothing(self) -> None:
        assert companion_suggestion_line([]) == ""

    def test_companions_render_as_a_soft_optional_line(self) -> None:
        line = companion_suggestion_line(["writing-plans", "ac-python"])
        assert "/writing-plans" in line
        assert "/ac-python" in line
        # Clearly SOFT — distinct from the mandatory LOAD directive.
        assert "optional" in line.lower()
        assert "LOAD THESE SKILLS NOW" not in line


class TestRenderSkillSuggestionMessage:
    def test_companion_is_surfaced_but_never_written_to_pending(self, tmp_path: Path) -> None:
        pending = tmp_path / "pending"
        message = render_skill_suggestion_message(
            {"suggestions": ["code"], "advisory": [], "companions": ["writing-plans"]},
            pending=pending,
            t3_reminder="",
            normalize=lambda name: name,
        )
        assert "LOAD THESE SKILLS NOW" in message
        assert "/code" in message
        assert "/writing-plans" in message  # surfaced ...
        assert "writing-plans" not in pending.read_text(encoding="utf-8")  # ... but not a hard demand

    def test_companion_surfaces_even_with_no_hard_suggestions(self, tmp_path: Path) -> None:
        # A companion of an already-loaded skill: no hard suggestion remains, yet
        # the companion is still surfaced (no mandatory directive is rendered).
        pending = tmp_path / "pending"
        message = render_skill_suggestion_message(
            {"suggestions": [], "advisory": [], "companions": ["ac-python"]},
            pending=pending,
            t3_reminder="",
            normalize=lambda name: name,
        )
        assert "/ac-python" in message
        assert "LOAD THESE SKILLS NOW" not in message


class TestCompanionSurfacedEndToEnd:
    """End-to-end through the live ``handle_user_prompt_submit`` handler."""

    @pytest.fixture
    def state_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        original = router.STATE_DIR
        router.STATE_DIR = tmp_path / "state"
        router.STATE_DIR.mkdir(parents=True, exist_ok=True)
        # #256: the suggester is gated on engagement; opt in via autoload.
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        monkeypatch.setattr(
            skill_loader_mod,
            "suggest_skills",
            lambda _input: {"suggestions": ["code"], "advisory": [], "companions": ["writing-plans"]},
        )
        yield router.STATE_DIR
        router.STATE_DIR = original

    def _pending(self, session_id: str) -> list[str]:
        path = router.STATE_DIR / f"{session_id}.pending"
        if not path.is_file():
            return []
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_companion_line_printed_and_not_hard_demanded(
        self, state_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        handle_user_prompt_submit({"session_id": "sess-comp", "prompt": "fix the bug"})
        message = capsys.readouterr().out
        assert "writing-plans" in message  # surfaced through the renderer ...
        assert "writing-plans" not in self._pending("sess-comp")  # ... never a hard demand
