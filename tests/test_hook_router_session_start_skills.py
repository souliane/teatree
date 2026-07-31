"""``autoload = true`` must put the context skills in the FIRST turn's context (#3869).

The defect: the only skill-selection path was ``handle_user_prompt_submit``, which returns
early without a prompt and is wired to ``UserPromptSubmit``. So the skills arrived AFTER
the first message was answered, and a session that never receives a ``UserPromptSubmit``
got nothing at all. ``handle_session_start_bootstrap`` armed loops and the statusline and
injected no skills.

The cases below pin the injection, its default-OFF half, and the never-lockout property —
in particular that a failure inside the suggester degrades to silence rather than to a
SessionStart that cannot complete.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest

from hooks.scripts import hook_router, session_start_skills


@pytest.fixture
def state_dir(tmp_path: Path) -> Iterator[Path]:
    # ``_merge_session_start_context`` folds in the hand-off drain and snapshot recovery,
    # both of which read the DB. Neutralised to the identity so these cases exercise the
    # skill injection alone; the merge itself has its own tests.
    with (
        mock.patch.object(hook_router, "STATE_DIR", tmp_path),
        mock.patch.object(hook_router, "_merge_session_start_context", side_effect=lambda ctx, *_a, **_k: ctx),
    ):
        yield tmp_path


def _emitted_context(capsys: pytest.CaptureFixture[str]) -> str:
    """The ``additionalContext`` of the ONE SessionStart stdout write, or ``""``."""
    out = capsys.readouterr().out.strip()
    if not out:
        return ""
    payload = json.loads(out.splitlines()[-1])
    return payload.get("hookSpecificOutput", {}).get("additionalContext", "")


def _bootstrap(session_id: str = "s1", source: str = "startup") -> None:
    hook_router.handle_session_start_bootstrap({"session_id": session_id, "source": source})


class TestAutoloadInjectsSkillsAtSessionStart:
    def test_the_context_skills_are_named_in_the_session_start_context(
        self, state_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            mock.patch.object(hook_router, "_autoload_enabled", return_value=True),
            mock.patch.object(hook_router, "_loop_auto_load_active", return_value=False),
            mock.patch.object(
                session_start_skills,
                "_suggest",
                return_value={"suggestions": ["ac-django", "ac-python"], "advisory": [], "companions": []},
            ),
        ):
            _bootstrap()
        context = _emitted_context(capsys)
        assert "/ac-django" in context
        assert "/ac-python" in context

    def test_the_demand_set_is_recorded_for_the_pretooluse_gate(
        self, state_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Same seam the UserPromptSubmit path writes, so there is ONE demand set rather
        # than a second answer to "which skills apply".
        with (
            mock.patch.object(hook_router, "_autoload_enabled", return_value=True),
            mock.patch.object(hook_router, "_loop_auto_load_active", return_value=False),
            mock.patch.object(
                session_start_skills,
                "_suggest",
                return_value={"suggestions": ["ac-django"], "advisory": [], "companions": []},
            ),
        ):
            _bootstrap()
        _emitted_context(capsys)
        assert (state_dir / "s1.pending").read_text(encoding="utf-8").split() == ["ac-django"]

    def test_the_statusline_seed_does_not_suppress_the_injection(
        self, state_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The autoload path seeds `<session>.skills` for the statusline. If the selection
        # ran AFTER that seed it would read those names as already loaded and suggest
        # nothing — a seed that marks skills loaded which were never loaded.
        captured: dict[str, object] = {}

        def _record(payload: dict) -> dict:
            captured["loaded"] = list(payload.get("loaded_skills", []))
            return {"suggestions": ["t3:internals"], "advisory": [], "companions": []}

        with (
            mock.patch.object(hook_router, "_autoload_enabled", return_value=True),
            mock.patch.object(hook_router, "_loop_auto_load_active", return_value=False),
            mock.patch.object(session_start_skills, "_suggest", side_effect=_record),
        ):
            _bootstrap()
        _emitted_context(capsys)
        assert captured["loaded"] == [], "the selection saw the statusline seed as loaded skills"


class TestDefaultOffAndNeverLockoutAreUnchanged:
    def test_a_session_without_autoload_gets_no_skill_injection(
        self, state_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            mock.patch.object(hook_router, "_autoload_enabled", return_value=False),
            mock.patch.object(hook_router, "_teatree_active", return_value=False),
            mock.patch.object(
                session_start_skills,
                "_suggest",
                return_value={"suggestions": ["ac-django"], "advisory": [], "companions": []},
            ),
        ):
            _bootstrap()
        assert "/ac-django" not in _emitted_context(capsys)
        assert not (state_dir / "s1.pending").exists()

    def test_a_failing_suggester_degrades_to_silence_rather_than_breaking_session_start(
        self, state_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # never-lockout: a failing selection must not propagate. SessionStart also carries
        # loop bootstrap and the parked hand-off drain, and a raise here takes both out.
        with (
            mock.patch.object(hook_router, "_autoload_enabled", return_value=True),
            mock.patch.object(hook_router, "_loop_auto_load_active", return_value=False),
            mock.patch.object(session_start_skills, "_suggest", side_effect=RuntimeError("boom")),
        ):
            _bootstrap()  # must not raise
        assert "LOAD THESE SKILLS" not in _emitted_context(capsys)
        # And the session is still ENGAGED — the failure cost the hint, not the engagement.
        assert (state_dir / "s1.teatree-active").is_file()

    def test_no_suggestions_writes_no_pending_demand(self, state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # The foil that keeps the gate from becoming a lockout: an empty selection must
        # leave `.pending` empty, or every tool call would block on an empty demand.
        with (
            mock.patch.object(hook_router, "_autoload_enabled", return_value=True),
            mock.patch.object(hook_router, "_loop_auto_load_active", return_value=False),
            mock.patch.object(
                session_start_skills,
                "_suggest",
                return_value={"suggestions": [], "advisory": [], "companions": []},
            ),
        ):
            _bootstrap()
        _emitted_context(capsys)
        pending = state_dir / "s1.pending"
        assert not pending.exists() or not pending.read_text(encoding="utf-8").strip()


class TestTheSelectionUsesTheExistingResolver:
    def test_the_prompt_is_empty_so_no_free_text_scan_happens(self, state_dir: Path) -> None:
        # `suggest_skills` uses the prompt ONLY for the loose supplementary keyword regexes,
        # which over-fire (#1567). At SessionStart there is no intent text to scan, so the
        # cwd/overlay context is the whole selection — and that must be explicit, not
        # incidental.
        captured: dict[str, object] = {}

        def _record(payload: dict) -> dict:
            captured["prompt"] = payload.get("prompt")
            return {"suggestions": [], "advisory": [], "companions": []}

        with mock.patch.object(session_start_skills, "_suggest", side_effect=_record):
            session_start_skills.session_start_skill_context("s1")
        assert captured["prompt"] == ""
