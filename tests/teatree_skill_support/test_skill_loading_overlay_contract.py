"""``SkillLoadingPolicy`` loads an overlay's DECLARED skills + the file-domain skill.

Overlay-agnostic, anti-vacuous coverage of the config-driven skill-loading
contract, using a SYNTHETIC overlay config and synthetic skill names.

Case 1 — a synthetic overlay declaring companion set ``S`` → the policy
resolves ``S`` for that overlay's work (the overlay declaration drives the
result, threaded through ``OverlayConfig.get_lifecycle_companion_skills``
exactly as the production dispatch path feeds ``companion_skills=`` to the
policy).

Case 2 — a Python-file domain → the (core) language skill ``ac-python``; a
Django-change domain → the (core) framework skill ``ac-django``. This
language/framework mapping is CORE, not overlay-config-provided, so the policy
honours the core mapping regardless of what an overlay declares.

Case 3 — anti-vacuity teeth, stated per case:

-   an overlay declaring NO companions resolves none of ``S`` — so the case-1
    positive genuinely depends on the declaration;
-   removing the overlay from scope (no remote match, not active) drops ``S``
    even when declared;
-   a directory with NO Python/Django marker yields no framework skill — so
    the case-2 positive depends on the domain marker;
-   flipping the marker (``django`` dep → plain Python) flips the resolved
    framework skill ``ac-django`` → ``ac-python``;
-   breaking the resolution (the overlay-scope predicate forced ``False``)
    flips every case-1 GREEN assertion RED.
"""

from pathlib import Path

import pytest

from teatree.core.overlay import OverlayConfig
from teatree.skill_support.loading import SkillLoadingPolicy

# Synthetic overlay metadata — no real overlay or private-skill names. The
# overlay's own skill is ``t3:synth``; its remote matches ``*synth-product*``.
_OVERLAY_META = {"skill_path": "t3:synth", "remote_patterns": ["*synth-product*"]}
# The skill set ``S`` a synthetic overlay declares it requires for its work.
_DECLARED_S = ["synth-conventions", "synth-domain-rules"]


def _overlay_config(declared: list[str]) -> OverlayConfig:
    config = OverlayConfig()
    config.pr_review_companion = ""
    config.companion_skills = list(declared)
    return config


class TestOverlayDeclaredSkillsResolve:
    """Case 1: an overlay declaring set ``S`` → the policy resolves ``S``."""

    def test_declared_companion_set_resolved_for_overlay_work(self, tmp_path: Path) -> None:
        config = _overlay_config(_DECLARED_S)
        declared = config.get_lifecycle_companion_skills("code")
        assert declared == _DECLARED_S

        result = SkillLoadingPolicy().select_for_runtime_phase(
            cwd=tmp_path,
            phase="coding",
            overlay_skill_metadata=_OVERLAY_META,
            companion_skills=declared,
        )
        assert "t3:synth" in result.skills
        for skill in _DECLARED_S:
            assert skill in result.skills

    def test_overlay_declaring_no_skills_resolves_none_of_declared_set(self, tmp_path: Path) -> None:
        # ANTI-VACUITY TOOTH: an overlay that declares NO companions must
        # resolve none of the declared set. The discriminating input is the
        # declaration — the positive test above genuinely depends on it.
        config = _overlay_config([])
        declared = config.get_lifecycle_companion_skills("code")
        assert declared == []

        result = SkillLoadingPolicy().select_for_runtime_phase(
            cwd=tmp_path,
            phase="coding",
            overlay_skill_metadata=_OVERLAY_META,
            companion_skills=declared,
        )
        for skill in _DECLARED_S:
            assert skill not in result.skills

    def test_declared_skills_withheld_when_overlay_out_of_scope(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ANTI-VACUITY TOOTH: declared but the overlay is NOT in scope (the
        # cwd's remote does not match the overlay's patterns) → S is withheld.
        # This is the prompt-hook path, where overlay_active is always False,
        # so the in-scope decision rests on the remote match.
        monkeypatch.setattr(
            "teatree.skill_support.loading._matches_any_remote",
            lambda _cwd, _patterns: False,
        )
        config = _overlay_config(_DECLARED_S)
        result = SkillLoadingPolicy().select_for_prompt_hook(
            cwd=tmp_path,
            intent="code",
            overlay_skill_metadata=_OVERLAY_META,
            loaded_skills=set(),
            companion_skills=config.get_lifecycle_companion_skills("code"),
        )
        assert "t3:synth" not in result.skills
        for skill in _DECLARED_S:
            assert skill not in result.skills

    def test_declared_skills_loaded_when_overlay_in_scope(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Same prompt-hook path, but the cwd's remote DOES match → S loads.
        # The single flipped input vs. the withheld case above is the remote
        # match, which is the discriminating part of the in-scope decision.
        monkeypatch.setattr(
            "teatree.skill_support.loading._matches_any_remote",
            lambda _cwd, _patterns: True,
        )
        config = _overlay_config(_DECLARED_S)
        result = SkillLoadingPolicy().select_for_prompt_hook(
            cwd=tmp_path,
            intent="code",
            overlay_skill_metadata=_OVERLAY_META,
            loaded_skills=set(),
            companion_skills=config.get_lifecycle_companion_skills("code"),
        )
        assert "t3:synth" in result.skills
        for skill in _DECLARED_S:
            assert skill in result.skills

    def test_broken_resolution_flips_green_red(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ANTI-VACUITY TOOTH: break the resolution at its core seam — force the
        # overlay-scope predicate to report the overlay is never in scope — and
        # the case-1 GREEN assertions flip RED (the overlay skill + declared S
        # disappear) even though overlay_active was requested. This proves the
        # GREEN tests above depend on the resolution actually firing.
        monkeypatch.setattr(
            SkillLoadingPolicy,
            "_overlay_in_scope",
            staticmethod(lambda **_kwargs: False),
        )
        result = SkillLoadingPolicy().select_for_runtime_phase(
            cwd=tmp_path,
            phase="coding",
            overlay_skill_metadata=_OVERLAY_META,
            companion_skills=_DECLARED_S,
        )
        assert "t3:synth" not in result.skills
        for skill in _DECLARED_S:
            assert skill not in result.skills


class TestFileDomainMapsToLanguageSkill:
    """Case 2: a Python file → the language skill; a Django change → the framework skill.

    The mapping is CORE (``detect_framework_skills``), not overlay-config
    provided. The policy honours the core mapping; an empty overlay metadata
    keeps the only resolved skills the framework skill + the lifecycle skill.
    """

    def test_python_file_domain_requires_language_skill(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'synthpkg'\n", encoding="utf-8")
        result = SkillLoadingPolicy().select_for_prompt_hook(
            cwd=tmp_path,
            intent="code",
            overlay_skill_metadata={},
            loaded_skills=set(),
        )
        assert "ac-python" in result.skills
        assert "ac-django" not in result.skills

    def test_django_change_domain_requires_framework_skill(self, tmp_path: Path) -> None:
        (tmp_path / "manage.py").touch()
        result = SkillLoadingPolicy().select_for_prompt_hook(
            cwd=tmp_path,
            intent="code",
            overlay_skill_metadata={},
            loaded_skills=set(),
        )
        assert "ac-django" in result.skills

    def test_no_domain_marker_requires_no_framework_skill(self, tmp_path: Path) -> None:
        # ANTI-VACUITY TOOTH: a directory with NO Python/Django marker yields
        # no framework skill — so the two positives above depend on the marker.
        result = SkillLoadingPolicy().select_for_prompt_hook(
            cwd=tmp_path,
            intent="code",
            overlay_skill_metadata={},
            loaded_skills=set(),
        )
        assert "ac-python" not in result.skills
        assert "ac-django" not in result.skills
        # The lifecycle skill is unaffected — the domain mapping is orthogonal.
        assert "code" in result.skills

    @pytest.mark.parametrize(
        ("dependency", "expected_present", "expected_absent"),
        [
            ('dependencies = ["django>=4.2"]', "ac-django", "ac-python"),
            ('name = "synthpkg"', "ac-python", "ac-django"),
        ],
    )
    def test_flipping_marker_flips_framework_skill(
        self,
        tmp_path: Path,
        dependency: str,
        expected_present: str,
        expected_absent: str,
    ) -> None:
        # ANTI-VACUITY TOOTH: the single flipped input is the pyproject content
        # (a Django dependency vs. plain Python). Flipping it flips the resolved
        # framework skill ac-django <-> ac-python, proving the assertion's
        # discriminating part is the domain marker, not a constant.
        (tmp_path / "pyproject.toml").write_text(f"[project]\n{dependency}\n", encoding="utf-8")
        result = SkillLoadingPolicy().select_for_prompt_hook(
            cwd=tmp_path,
            intent="code",
            overlay_skill_metadata={},
            loaded_skills=set(),
        )
        assert expected_present in result.skills
        assert expected_absent not in result.skills
