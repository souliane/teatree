"""Per-overlay ``pr_review_companion`` injection on reviewer dispatch (#1135).

When a reviewer sub-agent is dispatched (``phase == "reviewing"``), the
overlay's ``pr_review_companion`` skill is injected alongside ``/t3:review``.
The global default is ``"code-review"``; the teatree overlay overrides it to
``"receiving-code-review"`` via the ``[overlays.t3-teatree] pr_review_companion``
entry in the DB-home overlays config.

The injection threads through:

*   ``OverlayConfig.pr_review_companion`` — the per-overlay field, default
    ``"code-review"``.
*   ``SkillLoadingPolicy.select_for_runtime_phase`` — accepts the value as
    a parameter and appends it when the phase resolves to the ``review``
    lifecycle skill. The companion goes through the standard
    ``resolve_requires`` chain so its own ``requires`` expand transitively
    (and a review companion with no SKILL.md warns, dispatch still proceeds).
*   ``teatree.agents.skill_bundle.resolve_skill_bundle`` — reads the active
    overlay's ``pr_review_companion`` and passes it to the policy, keeping
    the policy module free of a back-reference to ``teatree.core``.

The companion is *injected*, never *replaces* ``/t3:review``.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from teatree.agents import skill_bundle
from teatree.core.overlay import OverlayConfig
from teatree.skill_support.loading import SkillLoadingPolicy


def _config_with_companion(value: str | None) -> OverlayConfig:
    """Build an ``OverlayConfig`` whose t3-teatree table sets ``pr_review_companion``.

    Passing ``None`` stubs an empty toml table so the class default applies.
    """
    overrides: dict[str, object] = {}
    if value is not None:
        overrides["pr_review_companion"] = value
    mock_config = MagicMock()
    mock_config.raw = {"overlays": {"t3-teatree": overrides}}
    with patch("teatree.config.load_config", return_value=mock_config):
        return OverlayConfig(overlay_name="t3-teatree")


class TestOverlayConfigPrReviewCompanionField:
    """``pr_review_companion`` is a real ``OverlayConfig`` field, default ``"code-review"``."""

    def test_default_is_code_review(self) -> None:
        config = OverlayConfig()
        assert config.pr_review_companion == "code-review"

    def test_toml_override_sets_field(self) -> None:
        config = _config_with_companion("receiving-code-review")
        assert config.pr_review_companion == "receiving-code-review"

    def test_per_instance_isolation(self) -> None:
        first = _config_with_companion("receiving-code-review")
        second = OverlayConfig()
        assert first.pr_review_companion == "receiving-code-review"
        assert second.pr_review_companion == "code-review"


class TestSelectForRuntimePhaseInjectsPrReviewCompanion:
    """``select_for_runtime_phase`` injects ``pr_review_companion`` on review phase."""

    def test_review_phase_injects_companion(self, tmp_path: Path) -> None:
        policy = SkillLoadingPolicy()
        result = policy.select_for_runtime_phase(
            cwd=tmp_path,
            phase="reviewing",
            overlay_skill_metadata={},
            pr_review_companion="code-review",
        )
        assert "review" in result.skills
        assert "code-review" in result.skills

    def test_overlay_override_injects_receiving_code_review(self, tmp_path: Path) -> None:
        policy = SkillLoadingPolicy()
        result = policy.select_for_runtime_phase(
            cwd=tmp_path,
            phase="reviewing",
            overlay_skill_metadata={},
            pr_review_companion="receiving-code-review",
        )
        assert "review" in result.skills
        assert "receiving-code-review" in result.skills

    def test_non_review_phase_skips_companion(self, tmp_path: Path) -> None:
        policy = SkillLoadingPolicy()
        result = policy.select_for_runtime_phase(
            cwd=tmp_path,
            phase="coding",
            overlay_skill_metadata={},
            pr_review_companion="code-review",
        )
        assert "code-review" not in result.skills

    def test_empty_companion_is_noop(self, tmp_path: Path) -> None:
        policy = SkillLoadingPolicy()
        result = policy.select_for_runtime_phase(
            cwd=tmp_path,
            phase="reviewing",
            overlay_skill_metadata={},
            pr_review_companion="",
        )
        assert "review" in result.skills
        assert "code-review" not in result.skills

    def test_missing_companion_in_index_dispatch_still_proceeds(self, tmp_path: Path) -> None:
        """A review companion absent from the skill index warns, doesn't fail.

        ``resolve_requires`` passes an unknown skill through as-is when no index
        entry expands it — the policy still returns ``review`` alongside the
        companion name, never crashes.
        """
        policy = SkillLoadingPolicy()
        result = policy.select_for_runtime_phase(
            cwd=tmp_path,
            phase="reviewing",
            overlay_skill_metadata={},
            pr_review_companion="nonexistent-companion-skill",
            skill_index=[],
        )
        assert "review" in result.skills
        assert "nonexistent-companion-skill" in result.skills

    def test_companion_dedup_against_lifecycle_skill(self, tmp_path: Path) -> None:
        """If the companion *is* the lifecycle skill, the result is deduped."""
        policy = SkillLoadingPolicy()
        result = policy.select_for_runtime_phase(
            cwd=tmp_path,
            phase="reviewing",
            overlay_skill_metadata={},
            pr_review_companion="review",
        )
        assert result.skills.count("review") == 1


class TestResolveSkillBundleReadsOverlayPrReviewCompanion:
    """``resolve_skill_bundle`` reads ``pr_review_companion`` from the active overlay."""

    def test_resolve_skill_bundle_injects_overlay_pr_review_companion(self) -> None:
        mock_overlay = MagicMock()
        mock_overlay.config.companion_skills = []
        mock_overlay.config.pr_review_companion = "receiving-code-review"
        with (
            patch.object(skill_bundle, "get_overlay", return_value=mock_overlay, create=True),
            patch(
                "teatree.core.overlay_loader.get_overlay",
                return_value=mock_overlay,
            ),
        ):
            bundle = skill_bundle.resolve_skill_bundle(
                phase="reviewing",
                overlay_skill_metadata={},
            )
        assert "review" in bundle
        assert "receiving-code-review" in bundle

    def test_resolve_skill_bundle_default_is_code_review(self) -> None:
        mock_overlay = MagicMock()
        mock_overlay.config.companion_skills = []
        mock_overlay.config.pr_review_companion = "code-review"
        with patch(
            "teatree.core.overlay_loader.get_overlay",
            return_value=mock_overlay,
        ):
            bundle = skill_bundle.resolve_skill_bundle(
                phase="reviewing",
                overlay_skill_metadata={},
            )
        assert "code-review" in bundle

    def test_resolve_skill_bundle_no_overlay_no_companion(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        # No overlay reachable → companion omitted, only lifecycle skill present.
        with patch(
            "teatree.core.overlay_loader.get_overlay",
            side_effect=RuntimeError("no overlay"),
        ):
            bundle = skill_bundle.resolve_skill_bundle(
                phase="reviewing",
                overlay_skill_metadata={},
            )
        assert "review" in bundle
        assert "code-review" not in bundle
        assert "receiving-code-review" not in bundle
