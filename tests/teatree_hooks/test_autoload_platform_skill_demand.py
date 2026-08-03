"""``autoload`` makes the platform skill a HARD demand on every engaged session.

``autoload`` is the owner's standing "teatree is on for every session" opt-in.
Engagement alone only armed the suggester and the loop: nothing ever put the
platform skill itself in front of the agent, so the owner had to invoke it by
hand on every fresh session. The demand is computed at the engagement seam
(``hooks.scripts.engagement``) and merged into the ``UserPromptSubmit`` hard
suggestion set, so it lands in ``<session>.pending`` and the ``PreToolUse``
skill-loading gate enforces it.

Integration-leaning: the end-to-end class drives the live
``handle_user_prompt_submit`` handler against a real state dir under
``tmp_path`` and reads back the real ``<session>.pending`` file.
"""

import sys
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.engagement import PLATFORM_SKILL, autoload_skill_demand
from hooks.scripts.hook_router import handle_user_prompt_submit

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import skill_loader as skill_loader_mod  # noqa: E402 — import follows the sys.path insert above


class TestAutoloadSkillDemand:
    def test_autoload_off_demands_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "0")
        assert autoload_skill_demand([]) == []

    def test_autoload_on_demands_the_platform_skill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        assert autoload_skill_demand([]) == [PLATFORM_SKILL]

    @pytest.mark.parametrize(
        "loaded",
        [PLATFORM_SKILL, f"t3:{PLATFORM_SKILL}", f"skills/{PLATFORM_SKILL}/SKILL.md"],
    )
    def test_an_already_loaded_platform_skill_is_not_re_demanded(
        self, monkeypatch: pytest.MonkeyPatch, loaded: str
    ) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        # Every spelling the session's skills file can carry canonicalizes to the
        # same token, so none of them re-demands an already-held skill.
        assert autoload_skill_demand([loaded]) == []

    def test_an_unrelated_loaded_skill_does_not_satisfy_the_demand(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        assert autoload_skill_demand(["t3:code", "ac-python"]) == [PLATFORM_SKILL]


class TestUserPromptSubmitDemandsThePlatformSkill:
    """End-to-end through the live ``handle_user_prompt_submit`` handler."""

    @pytest.fixture
    def state_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        original = router.STATE_DIR
        router.STATE_DIR = tmp_path / "state"
        router.STATE_DIR.mkdir(parents=True, exist_ok=True)
        yield router.STATE_DIR
        router.STATE_DIR = original

    def _pending(self, session_id: str) -> list[str]:
        path = router.STATE_DIR / f"{session_id}.pending"
        if not path.is_file():
            return []
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line]

    def _canonical_platform_skill(self) -> str:
        return router.normalize_skill_name(PLATFORM_SKILL)

    def test_autoload_session_hard_demands_the_platform_skill(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The shape the owner actually saw: the suggester resolves the overlay's
        # own skill from the cold metadata cache and nothing else, because the
        # overlay's companion list needs a live overlay import the hook
        # interpreter cannot perform.
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        monkeypatch.setattr(
            skill_loader_mod,
            "suggest_skills",
            lambda _input: {"suggestions": ["some-overlay"], "advisory": [], "companions": []},
        )

        handle_user_prompt_submit({"session_id": "sess-autoload", "prompt": "fix the bug"})

        canonical = self._canonical_platform_skill()
        assert canonical in self._pending("sess-autoload")
        assert f"/{canonical}" in capsys.readouterr().out

    def test_the_demand_survives_a_broken_suggester(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A suggester that raises used to degrade the whole prompt hook to
        # silence — indistinguishable from "the operator never opted in".
        monkeypatch.setenv("T3_AUTOLOAD", "1")

        def _boom(_input: dict) -> dict:
            msg = "suggester exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr(skill_loader_mod, "suggest_skills", _boom)

        handle_user_prompt_submit({"session_id": "sess-broken", "prompt": "fix the bug"})

        canonical = self._canonical_platform_skill()
        assert canonical in self._pending("sess-broken")
        assert f"/{canonical}" in capsys.readouterr().out

    def test_a_manually_engaged_session_is_not_forced_to_load_it(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Engagement via a loaded lifecycle skill is NOT the owner's standing
        # opt-in, so the platform skill stays a suggestion the agent may skip.
        monkeypatch.setenv("T3_AUTOLOAD", "0")
        (state_dir / "sess-manual.t3-engaged").touch()
        monkeypatch.setattr(
            skill_loader_mod,
            "suggest_skills",
            lambda _input: {"suggestions": ["some-overlay"], "advisory": [], "companions": []},
        )

        handle_user_prompt_submit({"session_id": "sess-manual", "prompt": "fix the bug"})

        assert self._canonical_platform_skill() not in self._pending("sess-manual")

    def test_an_already_loaded_platform_skill_is_not_re_demanded(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        canonical = self._canonical_platform_skill()
        (state_dir / "sess-held.skills").write_text(f"{canonical}\n", encoding="utf-8")
        monkeypatch.setattr(
            skill_loader_mod,
            "suggest_skills",
            lambda _input: {"suggestions": [], "advisory": [], "companions": []},
        )

        handle_user_prompt_submit({"session_id": "sess-held", "prompt": "fix the bug"})

        assert self._pending("sess-held") == []
