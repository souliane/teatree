"""Per-stage overlay skills embed IN FULL for a no-Skill-tool maker dispatch.

A maker agent type (``t3:coder``/``t3:debugger``/``t3:tester``/``t3:e2e``/
``t3:shipper``) has no Skill tool, so a stage skill only exists for it if its
``SKILL.md`` body is embedded IN FULL in the dispatched system context — a
one-line "available — load if needed" summary is vacuous. These tests use a
SYNTHETIC overlay declaring ``stage_skills={"coding": [<sentinel skill>]}`` and
assert the sentinel body appears in full in the coding-phase system context.

Anti-vacuity spine (RED before the wiring):

*   (a) the sentinel body is present IN FULL for the coding phase — RED when the
    stage skill is demoted to the summary line or never added to the bundle;
*   (b) a stage skill is appended AFTER the base and a duplicate of a base skill
    dedupes to the earlier (base) position — base stays authoritative;
*   (c) an overlay whose stage map is empty (a public dispatch) does NOT leak a
    different overlay's stage skill — the map is read from the ACTIVE overlay
    only;
*   (d) an unconfigured phase adds nothing.
"""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase
from pydantic import ValidationError

from teatree.agents import prompt, skill_injection
from teatree.agents.skill_bundle import active_overlay_stage_skills, resolve_skill_bundle
from teatree.core.models import Session, Task, Ticket
from teatree.core.overlay import OverlayConfig
from teatree.skill_support.loading import SkillLoadingPolicy

_STAGE_SENTINEL = "STAGE-SKILL-SENTINEL: additive per-stage overlay skill body, phase-scoped"
_STAGE_SKILL_NAME = "overlay-stage-conventions"
_OVERLAY_META = {"skill_path": "t3:synth", "remote_patterns": ["*synth-product*"]}


def _seed_skill(skills_dir: Path, name: str, *, body: str) -> None:
    skill = skills_dir / name
    skill.mkdir(parents=True, exist_ok=True)
    content = "\n".join(
        ["---", f"name: {name}", 'description: "Synthetic stage skill."', "---", f"# {name}", "", body, ""]
    )
    (skill / "SKILL.md").write_text(content, encoding="utf-8")


@pytest.fixture
def skills_dir(tmp_path: Path) -> Iterator[Path]:
    """Seed a synthetic skills tree and point the injection module at it."""
    sd = tmp_path / "skills"
    _seed_skill(sd, "code", body="# code lifecycle skill")
    _seed_skill(sd, "test", body="# test lifecycle skill")
    _seed_skill(sd, "rules", body="# rules")
    _seed_skill(sd, _STAGE_SKILL_NAME, body=_STAGE_SENTINEL)
    with patch.object(skill_injection, "DEFAULT_SKILLS_DIR", sd):
        yield sd


def _overlay_with_stage_map(stage_map: dict[str, list[str]]) -> MagicMock:
    overlay = MagicMock()
    overlay.config.companion_skills = []
    overlay.config.pr_review_companion = ""
    overlay.config.get_review_companion_skills.return_value = []
    overlay.config.get_stage_skills.side_effect = lambda phase: list(stage_map.get(phase, []))
    return overlay


class TestOverlayConfigStageSkills:
    """``stage_skills`` is a typed, key-validated, phase-canonicalized map."""

    def test_default_is_empty(self) -> None:
        config = OverlayConfig()
        assert config.stage_skills == {}
        assert config.get_stage_skills("coding") == []

    def test_get_stage_skills_returns_configured(self) -> None:
        config = OverlayConfig()
        config.stage_skills = {"coding": ["backend-dev", "frontend-dev"]}
        assert config.get_stage_skills("coding") == ["backend-dev", "frontend-dev"]

    def test_keys_canonicalized_in_storage(self) -> None:
        config = OverlayConfig()
        config.stage_skills = {"code": ["backend-dev"], "review": ["elite-review"]}
        assert config.stage_skills == {"coding": ["backend-dev"], "reviewing": ["elite-review"]}

    def test_get_stage_skills_normalizes_lookup_phase(self) -> None:
        config = OverlayConfig()
        config.stage_skills = {"reviewing": ["elite-review"]}
        assert config.get_stage_skills("review") == ["elite-review"]

    def test_unconfigured_phase_returns_empty(self) -> None:
        config = OverlayConfig()
        config.stage_skills = {"coding": ["backend-dev"]}
        assert config.get_stage_skills("testing") == []

    def test_reactive_phase_key_accepted(self) -> None:
        config = OverlayConfig()
        config.stage_skills = {"debugging": ["bug-report"]}
        assert config.get_stage_skills("debugging") == ["bug-report"]

    def test_unknown_phase_key_rejected(self) -> None:
        config = OverlayConfig()
        with pytest.raises(ValidationError):
            config.stage_skills = {"nonsense-phase": ["elite-review"]}

    def test_empty_skill_names_filtered(self) -> None:
        config = OverlayConfig()
        config.stage_skills = {"coding": ["backend-dev", ""]}
        assert config.get_stage_skills("coding") == ["backend-dev"]


class TestActiveOverlayStageSkills:
    """``active_overlay_stage_skills`` reads the active overlay's ``get_stage_skills``."""

    def test_reads_overlay_hook(self) -> None:
        overlay = _overlay_with_stage_map({"coding": [_STAGE_SKILL_NAME]})
        with patch("teatree.core.overlay_loader.get_overlay", return_value=overlay):
            assert active_overlay_stage_skills("coding") == [_STAGE_SKILL_NAME]

    def test_no_overlay_returns_empty(self) -> None:
        with patch("teatree.core.overlay_loader.get_overlay", side_effect=RuntimeError("no overlay")):
            assert active_overlay_stage_skills("coding") == []

    def test_overlay_without_hook_returns_empty(self) -> None:
        overlay = MagicMock()
        overlay.config = object()
        with patch("teatree.core.overlay_loader.get_overlay", return_value=overlay):
            assert active_overlay_stage_skills("coding") == []

    def test_unresolvable_stage_skill_warns_and_still_returns(self, caplog: pytest.LogCaptureFixture) -> None:
        overlay = _overlay_with_stage_map({"coding": ["definitely-not-a-real-skill-xyz"]})
        with patch("teatree.core.overlay_loader.get_overlay", return_value=overlay):
            result = active_overlay_stage_skills("coding")
        assert result == ["definitely-not-a-real-skill-xyz"]
        assert any("definitely-not-a-real-skill-xyz" in record.message for record in caplog.records)


class TestSelectForRuntimePhaseStageSkills:
    """The policy appends stage skills LAST, base-wins on any duplicate (requirement b)."""

    def test_stage_skill_appended_after_lifecycle_skill(self, tmp_path: Path) -> None:
        result = SkillLoadingPolicy().select_for_runtime_phase(
            cwd=tmp_path,
            phase="coding",
            overlay_skill_metadata=_OVERLAY_META,
            stage_skills=["stage-only"],
        )
        assert "stage-only" in result.skills
        assert result.skills.index("stage-only") > result.skills.index("code")

    def test_stage_skill_duplicating_base_dedupes_base_wins(self, tmp_path: Path) -> None:
        result = SkillLoadingPolicy().select_for_runtime_phase(
            cwd=tmp_path,
            phase="coding",
            overlay_skill_metadata={},
            companion_skills=["shared"],
            stage_skills=["shared", "stage-only"],
        )
        assert result.skills.count("shared") == 1
        assert result.skills.index("shared") < result.skills.index("stage-only")

    def test_no_stage_skills_is_byte_identical(self, tmp_path: Path) -> None:
        without = SkillLoadingPolicy().select_for_runtime_phase(
            cwd=tmp_path, phase="coding", overlay_skill_metadata=_OVERLAY_META
        )
        with_empty = SkillLoadingPolicy().select_for_runtime_phase(
            cwd=tmp_path, phase="coding", overlay_skill_metadata=_OVERLAY_META, stage_skills=[]
        )
        assert without.skills == with_empty.skills


class TestStageSkillEmbeddedInFullConformance(TestCase):
    """The coding-phase system context embeds the overlay stage skill IN FULL.

    RED before the wiring: the stage skill is either absent from the bundle or
    demoted to the ``"available — load if needed"`` summary and the sentinel
    body never reaches the no-Skill-tool builder.
    """

    def _task(self, phase: str, state: str) -> Task:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=state)
        session = Session.objects.create(ticket=ticket, agent_id=f"maker:{phase}")
        return Task.objects.create(ticket=ticket, session=session, phase=phase)

    @pytest.mark.usefixtures("skills_dir")
    def test_coding_phase_embeds_stage_skill_in_full(self) -> None:
        overlay = _overlay_with_stage_map({"coding": [_STAGE_SKILL_NAME]})
        task = self._task("coding", Ticket.State.STARTED)
        with patch("teatree.core.overlay_loader.get_overlay", return_value=overlay):
            context = prompt.build_system_context(
                task,
                skills=["code", "rules", _STAGE_SKILL_NAME],
                lifecycle_skill="code",
            )
        assert _STAGE_SENTINEL in context
        assert f"- {_STAGE_SKILL_NAME}: available — load if needed" not in context

    @pytest.mark.usefixtures("skills_dir")
    def test_coding_phase_carries_additive_precedence_line(self) -> None:
        overlay = _overlay_with_stage_map({"coding": [_STAGE_SKILL_NAME]})
        task = self._task("coding", Ticket.State.STARTED)
        with patch("teatree.core.overlay_loader.get_overlay", return_value=overlay):
            context = prompt.build_system_context(
                task,
                skills=["code", "rules", _STAGE_SKILL_NAME],
                lifecycle_skill="code",
            )
        assert "STAGE CUSTOM SKILLS" in context
        assert "ADDITIVE" in context
        # The stage skill is embedded in full, not routed to the force-load
        # block a no-Skill-tool builder cannot act on.
        assert f"  - /{_STAGE_SKILL_NAME}" not in context

    @pytest.mark.usefixtures("skills_dir")
    def test_base_lifecycle_skill_still_present(self) -> None:
        overlay = _overlay_with_stage_map({"coding": [_STAGE_SKILL_NAME]})
        task = self._task("coding", Ticket.State.STARTED)
        with patch("teatree.core.overlay_loader.get_overlay", return_value=overlay):
            context = prompt.build_system_context(
                task,
                skills=["code", "rules", _STAGE_SKILL_NAME],
                lifecycle_skill="code",
            )
        assert "# code lifecycle skill" in context
        assert _STAGE_SENTINEL in context

    @pytest.mark.usefixtures("skills_dir")
    def test_out_of_scope_overlay_does_not_leak(self) -> None:
        # Anti-vacuity contrast: the SAME coding-phase resolution under a public
        # overlay (empty stage map) excludes the sentinel skill that a different
        # overlay's map would supply, while the team overlay includes it.
        public = _overlay_with_stage_map({})
        with patch("teatree.core.overlay_loader.get_overlay", return_value=public):
            public_bundle = resolve_skill_bundle(phase="coding", overlay_skill_metadata=_OVERLAY_META)
        assert _STAGE_SKILL_NAME not in public_bundle

        team = _overlay_with_stage_map({"coding": [_STAGE_SKILL_NAME]})
        with patch("teatree.core.overlay_loader.get_overlay", return_value=team):
            team_bundle = resolve_skill_bundle(phase="coding", overlay_skill_metadata=_OVERLAY_META)
        assert _STAGE_SKILL_NAME in team_bundle

    @pytest.mark.usefixtures("skills_dir")
    def test_unconfigured_phase_adds_nothing(self) -> None:
        overlay = _overlay_with_stage_map({"coding": [_STAGE_SKILL_NAME]})
        with patch("teatree.core.overlay_loader.get_overlay", return_value=overlay):
            coding_bundle = resolve_skill_bundle(phase="coding", overlay_skill_metadata=_OVERLAY_META)
            testing_bundle = resolve_skill_bundle(phase="testing", overlay_skill_metadata=_OVERLAY_META)
        assert _STAGE_SKILL_NAME in coding_bundle
        assert _STAGE_SKILL_NAME not in testing_bundle
