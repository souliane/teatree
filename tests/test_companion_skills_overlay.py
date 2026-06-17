"""Per-overlay ``companion_skills`` propagation through the skill-loading policy.

When an overlay declares ``companion_skills = ["ac-django", "ac-python"]`` in
its ``[overlays.<name>]`` table of ``~/.teatree.toml``, those skills must
appear in the resolved suggestion set for both the UserPromptSubmit hook
path (``select_for_prompt_hook``) and the runtime-phase path
(``select_for_runtime_phase``).

The wiring threads through ``OverlayConfig.apply_toml_overrides`` (reads the
field from the ``[overlays.<name>]`` table and sets it on the instance),
``SkillLoadingPolicy._base_detected_skills`` (accepts an explicit
``companion_skills`` list that the caller — the UserPromptSubmit script or
``resolve_skill_bundle`` — reads from the active overlay's config and passes
in, keeping the policy module free of a back-reference to ``teatree.core``),
and ``resolve_companions`` (the existing resolver, which handles transitive
``requires``/``companions`` so we never parallel-implement the dep chain).

Reuses ``resolve_companions`` — does NOT parallel-implement a
teatree-specific path regex or single-purpose handler (the approach taken by
the reverted commit ``ae66b291``).
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from teatree.core.overlay import OverlayConfig
from teatree.skill_support.loading import SkillLoadingPolicy


def _config_with_companions(companions: list[str]) -> OverlayConfig:
    """Build an ``OverlayConfig`` whose t3-teatree table sets companion_skills.

    Stubs ``teatree.config.load_config`` so the toml lookup is hermetic.
    """
    mock_config = MagicMock()
    mock_config.raw = {"overlays": {"t3-teatree": {"companion_skills": companions}}}
    with patch("teatree.config.load_config", return_value=mock_config):
        return OverlayConfig(overlay_name="t3-teatree")


class TestOverlayConfigCompanionSkillsField:
    """``companion_skills`` is a real ``OverlayConfig`` field, default empty."""

    def test_default_is_empty_list(self) -> None:
        config = OverlayConfig()
        assert config.companion_skills == []

    def test_toml_overrides_set_field(self) -> None:
        config = _config_with_companions(["ac-django", "ac-python"])
        assert config.companion_skills == ["ac-django", "ac-python"]

    def test_per_instance_isolation(self) -> None:
        # Two configs must not share the same mutable list — tested indirectly
        # by mutating one and reading the other.
        first = OverlayConfig()
        second = OverlayConfig()
        first.companion_skills.append("rules")
        assert second.companion_skills == []


# An overlay whose remote_patterns match the cwd → the prompt-hook path resolves
# the overlay as in-scope, so its companion skills are required. Without a
# matching remote, the overlay is NOT in scope and the companions are withheld.
_IN_SCOPE_OVERLAY_META = {"skill_path": "t3:acme", "remote_patterns": ["*acme*"]}


class TestSelectForPromptHookEmitsCompanionSkills:
    """``select_for_prompt_hook`` emits the overlay's ``companion_skills``."""

    def test_prompt_hook_includes_overlay_companion_skills(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "teatree.skill_support.loading._matches_any_remote",
            lambda _cwd, _patterns: True,
        )
        config = _config_with_companions(["ac-django", "ac-python"])
        trigger_index: list[dict[str, object]] = [
            {"skill": "code", "companions": [], "requires": []},
        ]
        policy = SkillLoadingPolicy()
        result = policy.select_for_prompt_hook(
            cwd=tmp_path,
            intent="code",
            overlay_skill_metadata=_IN_SCOPE_OVERLAY_META,
            loaded_skills=set(),
            trigger_index=trigger_index,
            companion_skills=config.companion_skills,
        )
        assert "ac-django" in result.skills
        assert "ac-python" in result.skills

    def test_prompt_hook_dedupes_overlay_companions_with_framework_detect(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        # Even if framework detection would also pick ``ac-django`` (manage.py
        # present), the overlay-declared companion must not appear twice.
        monkeypatch.setattr(
            "teatree.skill_support.loading._matches_any_remote",
            lambda _cwd, _patterns: True,
        )
        (tmp_path / "manage.py").touch()
        config = _config_with_companions(["ac-django", "ac-python"])
        trigger_index: list[dict[str, object]] = [
            {"skill": "code", "companions": [], "requires": []},
        ]
        policy = SkillLoadingPolicy()
        result = policy.select_for_prompt_hook(
            cwd=tmp_path,
            intent="code",
            overlay_skill_metadata=_IN_SCOPE_OVERLAY_META,
            loaded_skills=set(),
            trigger_index=trigger_index,
            companion_skills=config.companion_skills,
        )
        assert result.skills.count("ac-django") == 1
        assert "ac-python" in result.skills


class TestSelectForRuntimePhaseEmitsCompanionSkills:
    """``select_for_runtime_phase`` emits the overlay's ``companion_skills``."""

    def test_runtime_phase_includes_overlay_companion_skills(self, tmp_path: Path) -> None:
        config = _config_with_companions(["ac-django", "ac-python"])
        trigger_index: list[dict[str, object]] = [
            {"skill": "code", "companions": [], "requires": []},
        ]
        policy = SkillLoadingPolicy()
        result = policy.select_for_runtime_phase(
            cwd=tmp_path,
            phase="coding",
            overlay_skill_metadata={},
            trigger_index=trigger_index,
            companion_skills=config.companion_skills,
        )
        assert "ac-django" in result.skills
        assert "ac-python" in result.skills
