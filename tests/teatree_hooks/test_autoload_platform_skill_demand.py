"""``autoload`` makes the platform skill a HARD demand on every engaged ATTENDED session.

``autoload`` is the owner's standing "teatree is on for every session" opt-in.
Engagement alone only armed the suggester and the loop: nothing ever put the
platform skill itself in front of the agent, so the owner had to invoke it by
hand on every fresh session. The demand is computed at the engagement seam
(``hooks.scripts.engagement``) and merged into the ``UserPromptSubmit`` hard
suggestion set, so it lands in ``<session>.pending`` and the ``PreToolUse``
skill-loading gate enforces it.

That gate refuses every ``Edit``/``Write``/``Bash`` until the demand is
satisfied, and the factory's own SDK workers run the same hooks — so an
un-laned demand made the owner's opt-in a hard block on a skill that is Claude
Code harness wiring plus attended-session hygiene. Hence :class:`TestTheDemandIsLaneScoped`,
whose SDK cases are the ones that would go green on a uniformly-demanding seam
and whose attended cases are the ones that would go green on a uniformly-silent
one.

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
from hooks.scripts.session_lane import LANE_INTERACTIVE_CLI, LANE_SDK, LANE_UNKNOWN
from tests._lane_env import pin_lane

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import skill_loader as skill_loader_mod  # noqa: E402 — import follows the sys.path insert above


class TestTheDemandIsLaneScoped:
    def test_a_headless_sdk_run_is_never_demanded_the_platform_skill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        pin_lane(monkeypatch, LANE_SDK)
        assert autoload_skill_demand([]) == []

    def test_an_interactive_cli_session_is_demanded_the_platform_skill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        pin_lane(monkeypatch, LANE_INTERACTIVE_CLI)
        assert autoload_skill_demand([]) == [PLATFORM_SKILL]

    def test_an_unknown_lane_keeps_the_attended_demand(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        pin_lane(monkeypatch, LANE_UNKNOWN)
        assert autoload_skill_demand([]) == [PLATFORM_SKILL]

    @pytest.mark.parametrize("lane", [LANE_SDK, LANE_INTERACTIVE_CLI, LANE_UNKNOWN])
    def test_autoload_off_demands_nothing_in_any_lane(self, monkeypatch: pytest.MonkeyPatch, lane: str) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "0")
        pin_lane(monkeypatch, lane)
        assert autoload_skill_demand([]) == []


class TestAutoloadSkillDemand:
    @pytest.fixture(autouse=True)
    def _attended(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        pin_lane(monkeypatch, LANE_INTERACTIVE_CLI)

    @pytest.mark.parametrize(
        "loaded",
        [PLATFORM_SKILL, f"t3:{PLATFORM_SKILL}", f"skills/{PLATFORM_SKILL}/SKILL.md"],
    )
    def test_an_already_loaded_platform_skill_is_not_re_demanded(self, loaded: str) -> None:
        # Every spelling the session's skills file can carry canonicalizes to the
        # same token, so none of them re-demands an already-held skill.
        assert autoload_skill_demand([loaded]) == []

    def test_an_unrelated_loaded_skill_does_not_satisfy_the_demand(self) -> None:
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

    def test_a_headless_sdk_worker_gets_no_pending_platform_skill(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``<session>.pending`` is what the PreToolUse skill-loading gate reads,
        # so this is where the demand becomes a hard block on the factory.
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        pin_lane(monkeypatch, LANE_SDK)
        monkeypatch.setattr(
            skill_loader_mod,
            "suggest_skills",
            lambda _input: {"suggestions": [], "advisory": [], "companions": []},
        )

        handle_user_prompt_submit({"session_id": "sess-sdk", "prompt": "implement the fix"})

        assert self._canonical_platform_skill() not in self._pending("sess-sdk")

    def test_autoload_session_hard_demands_the_platform_skill(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The shape the owner actually saw: the suggester resolves the overlay's
        # own skill from the cold metadata cache and nothing else, because the
        # overlay's companion list needs a live overlay import the hook
        # interpreter cannot perform.
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        pin_lane(monkeypatch, LANE_INTERACTIVE_CLI)
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
        pin_lane(monkeypatch, LANE_INTERACTIVE_CLI)

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
        pin_lane(monkeypatch, LANE_INTERACTIVE_CLI)
        canonical = self._canonical_platform_skill()
        (state_dir / "sess-held.skills").write_text(f"{canonical}\n", encoding="utf-8")
        monkeypatch.setattr(
            skill_loader_mod,
            "suggest_skills",
            lambda _input: {"suggestions": [], "advisory": [], "companions": []},
        )

        handle_user_prompt_submit({"session_id": "sess-held", "prompt": "fix the bug"})

        assert self._pending("sess-held") == []
