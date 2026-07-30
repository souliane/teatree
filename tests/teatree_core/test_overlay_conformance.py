import importlib.metadata
import inspect

import pytest
from django.core.exceptions import ImproperlyConfigured

from teatree.contrib.t3_teatree.overlay import TeatreeOverlay
from teatree.core import overlay_loader
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_conformance import (
    _missing_params,
    conforming_or_none,
    conforming_or_raise,
    overlay_signature_violations,
)


def _sig(fn) -> inspect.Signature:
    return inspect.signature(fn)


class _ConformingMeta:
    def validate_pr(self, title: str, description: str, *, require_sections: bool = True):
        del title, description, require_sections
        return {"errors": [], "warnings": []}


class _DropsKeywordMeta:
    def validate_pr(self, title: str, description: str):
        del title, description
        return {"errors": [], "warnings": []}


class _AbsorbsKwargsMeta:
    def validate_pr(self, title: str, description: str, **kwargs):
        del title, description, kwargs
        return {"errors": [], "warnings": []}


class _RenamesPositionalReview:
    def visual_qa_targets(self, files: list[str]) -> list[str]:
        return list(files)


class _DropsPositionalReview:
    def visual_qa_targets(self) -> list[str]:
        return []


def _overlay_with(**facets: object) -> OverlayBase:
    class _Ov(OverlayBase):
        def get_repos(self) -> list[str]:
            return []

        def get_provision_steps(self, worktree):
            return []

    overlay = _Ov()
    for attr, facet in facets.items():
        setattr(overlay, attr, facet)
    return overlay


class TestOverlaySignatureViolations:
    def test_conforming_override_has_no_violations(self) -> None:
        overlay = _overlay_with(metadata=_ConformingMeta())
        assert overlay_signature_violations(overlay, name="alpha") == []

    def test_dropped_keyword_only_param_is_flagged(self) -> None:
        overlay = _overlay_with(metadata=_DropsKeywordMeta())
        violations = overlay_signature_violations(overlay, name="alpha")
        assert violations, "a validate_pr override dropping require_sections must be flagged"
        joined = " ".join(violations)
        assert "require_sections" in joined
        assert "validate_pr" in joined
        assert "alpha" in joined

    def test_var_keyword_absorbs_the_documented_keyword(self) -> None:
        overlay = _overlay_with(metadata=_AbsorbsKwargsMeta())
        assert overlay_signature_violations(overlay, name="alpha") == []

    def test_renamed_positional_param_is_not_flagged(self) -> None:
        overlay = _overlay_with(review=_RenamesPositionalReview())
        assert overlay_signature_violations(overlay, name="alpha") == []

    def test_dropped_positional_param_is_flagged(self) -> None:
        overlay = _overlay_with(review=_DropsPositionalReview())
        violations = overlay_signature_violations(overlay, name="alpha")
        assert any("visual_qa_targets" in v and "changed_files" in v for v in violations)

    def test_real_teatree_overlay_conforms(self) -> None:
        assert overlay_signature_violations(TeatreeOverlay(), name="t3-teatree") == []

    def test_overlay_base_level_hook_violation_is_flagged(self) -> None:
        # Built via type() so the deliberately narrow get_issue_title override is
        # not a lexical Liskov violation the type checker would reject; the runtime
        # signature is what the conformance scan reads.
        bad_cls = type(
            "_BadBaseHook",
            (OverlayBase,),
            {
                "get_repos": lambda self: [],
                "get_provision_steps": lambda self, worktree: [],
                "get_issue_title": lambda self: "",  # base declares (self, url)
            },
        )
        violations = overlay_signature_violations(bad_cls(), name="alpha")
        assert any("get_issue_title" in v and "url" in v for v in violations)

    def test_label_falls_back_to_class_name_without_name(self) -> None:
        overlay = _overlay_with(metadata=_DropsKeywordMeta())
        violations = overlay_signature_violations(overlay)
        assert violations
        assert all(type(overlay).__name__ in v for v in violations)


class TestMissingParams:
    def test_base_var_params_are_ignored(self) -> None:
        def base(a, b, *args, **kwargs): ...

        def override(a, b): ...

        assert _missing_params(_sig(base), _sig(override)) == []

    def test_var_args_and_kwargs_absorb_every_base_param(self) -> None:
        def base(a, *, kw): ...

        def override(*args, **kwargs): ...

        assert _missing_params(_sig(base), _sig(override)) == []

    def test_dropped_trailing_positional_is_missing(self) -> None:
        def base(a, b): ...

        def override(a): ...

        assert _missing_params(_sig(base), _sig(override)) == ["b"]

    def test_dropped_keyword_only_is_missing(self) -> None:
        def base(a, *, kw): ...

        def override(a): ...

        assert _missing_params(_sig(base), _sig(override)) == ["kw"]

    def test_renamed_positional_is_accepted(self) -> None:
        def base(a, b): ...

        def override(a, x): ...

        assert _missing_params(_sig(base), _sig(override)) == []

    def test_positional_or_keyword_satisfied_by_keyword_slot(self) -> None:
        def base(a, b): ...

        def override(a, *, b): ...

        assert _missing_params(_sig(base), _sig(override)) == []


class TestConformanceHelpers:
    def test_conforming_or_raise_returns_conforming_overlay(self) -> None:
        overlay = _overlay_with(metadata=_ConformingMeta())
        assert conforming_or_raise(overlay, "alpha") is overlay

    def test_conforming_or_raise_raises_on_violation(self) -> None:
        overlay = _overlay_with(metadata=_DropsKeywordMeta())
        with pytest.raises(ImproperlyConfigured) as exc:
            conforming_or_raise(overlay, "alpha")
        assert "require_sections" in str(exc.value)

    def test_conforming_or_none_returns_conforming_overlay(self) -> None:
        overlay = _overlay_with(metadata=_ConformingMeta())
        assert conforming_or_none(overlay, "alpha") is overlay

    def test_conforming_or_none_returns_none_and_warns(self, caplog) -> None:
        overlay = _overlay_with(metadata=_DropsKeywordMeta())
        with caplog.at_level("WARNING"):
            assert conforming_or_none(overlay, "alpha") is None
        assert "require_sections" in caplog.text


class TestDiscoveryEnforcesConformance:
    def test_entry_point_overlay_with_bad_signature_raises(self, monkeypatch) -> None:
        class _BadOverlay(OverlayBase):
            metadata = _DropsKeywordMeta()

            def get_repos(self) -> list[str]:
                return []

            def get_provision_steps(self, worktree):
                return []

        class _EP:
            name = "broken"
            value = "x:_BadOverlay"

            def load(self):
                return _BadOverlay

        monkeypatch.setattr(importlib.metadata, "entry_points", lambda *, group: [_EP()])
        monkeypatch.setattr(overlay_loader, "_discover_toml_overlays", lambda *a, **k: {})
        overlay_loader.reset_overlay_cache()
        with pytest.raises(ImproperlyConfigured) as exc:
            overlay_loader._discover_overlays()
        assert "broken" in str(exc.value)
        assert "require_sections" in str(exc.value)
        overlay_loader.reset_overlay_cache()
