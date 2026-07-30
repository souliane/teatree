"""Overlay-encapsulation fitness functions (enforce the downstream PR 8 fixes).

These two structural gates live in teatree and run against every overlay
registered on the test host (here only the bundled ``t3_teatree``); they enforce,
from teatree, the encapsulation invariants a downstream private overlay's PR 8
fixed source-side:

E5 — an overlay that overrides ``review.visual_qa_targets`` MUST also override
``classify_customer_display_impact`` (a VQA target with no display-impact
classifier is an incoherent half-declaration).

E7 — a class registered on the ``teatree.overlays`` entry point inherits ONLY
from ``OverlayBase`` (no ``*Mixin`` bases — the multi-mixin MRO PR 8 collapsed
to composition).

Both PASS on HEAD (the bundled overlay + scaffolder template conform). Each gate
carries a local-subclass anti-vacuity proof so the predicate is observed to BITE
without depending on a misconfigured registered overlay (which would make CI red
on this host). The registry-level assertion is the live gate; the local-subclass
assertion is the anti-vacuity proof.
"""

from importlib.metadata import entry_points

from teatree.core.overlay import OverlayBase, OverlayReview
from teatree.core.overlay_loader import get_all_overlays


def _review_is_overridden(review: object, hook_name: str) -> bool:
    base_method = getattr(OverlayReview, hook_name, None)
    inst_method = getattr(type(review), hook_name, None)
    if base_method is None or inst_method is None:
        return False
    return inst_method is not base_method


def _overrides_visual_qa_without_display_impact(overlay: object) -> bool:
    """The E5 violation predicate: VQA override without the display-impact override.

    PR-27b: both hooks live on the composed ``review`` facet, so the check
    inspects ``overlay.review`` against the ``OverlayReview`` defaults.
    """
    review = getattr(overlay, "review", None)
    if review is None:
        return False
    return _review_is_overridden(review, "visual_qa_targets") and not _review_is_overridden(
        review,
        "classify_customer_display_impact",
    )


# --- E5: visual_qa_targets ⇒ classify_customer_display_impact -----------------


def test_every_registered_overlay_pairs_visual_qa_with_display_impact() -> None:
    offenders = [
        name for name, overlay in get_all_overlays().items() if _overrides_visual_qa_without_display_impact(overlay)
    ]
    assert not offenders, (
        "overlay overrides review.visual_qa_targets but NOT review.classify_customer_display_impact "
        f"(E5 — a VQA target needs a display-impact classifier): {offenders}"
    )


def test_e5_predicate_bites_on_a_half_declared_overlay() -> None:
    # Anti-vacuity: a throwaway overlay whose review facet overrides only
    # visual_qa_targets MUST be flagged; one that overrides both MUST NOT.
    class _OnlyVisualQAReview(OverlayReview):
        def visual_qa_targets(self, changed_files: list[str]) -> list[str]:
            _ = changed_files
            return ["/"]

    class _OnlyVisualQA(OverlayBase):
        review = _OnlyVisualQAReview()

        def get_repos(self) -> list[str]:
            return []

        def get_provision_steps(self, worktree: object) -> list:
            _ = worktree
            return []

    assert _overrides_visual_qa_without_display_impact(_OnlyVisualQA())

    class _BothReview(OverlayReview):
        def visual_qa_targets(self, changed_files: list[str]) -> list[str]:
            _ = changed_files
            return ["/"]

        def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
            _ = changed_files
            return True

    class _BothOverridden(_OnlyVisualQA):
        review = _BothReview()

    assert not _overrides_visual_qa_without_display_impact(_BothOverridden())


# --- E7: entry-point overlays inherit ONLY from OverlayBase -------------------


def _has_mixin_base(cls: type) -> bool:
    """The E7 violation predicate: a base other than OverlayBase/object, or a *Mixin*."""
    for base in cls.__bases__:
        if base in {OverlayBase, object}:
            continue
        return True
    return any(base.__name__.endswith("Mixin") for base in cls.__bases__)


def test_every_entry_point_overlay_inherits_only_from_overlay_base() -> None:
    offenders: dict[str, list[str]] = {}
    for ep in entry_points(group="teatree.overlays"):
        cls = ep.load()
        if _has_mixin_base(cls):
            offenders[ep.name] = [b.__name__ for b in cls.__bases__]
    assert not offenders, (
        "entry-point overlay class has a non-OverlayBase base (E7 — entry-point overlays "
        f"compose, never mixin-inherit): {offenders}"
    )


def test_e7_predicate_bites_on_a_mixin_overlay() -> None:
    # Anti-vacuity: a class with a *Mixin* base MUST be flagged; a plain
    # OverlayBase subclass MUST NOT.
    class _Mixin:
        pass

    class _Bad(_Mixin, OverlayBase):
        pass

    class _Good(OverlayBase):
        pass

    assert _has_mixin_base(_Bad)
    assert not _has_mixin_base(_Good)
