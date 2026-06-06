"""Tests for the unified canonical-overlay-name resolver.

Issue souliane/teatree#1138: ``teatree.config._canonical_ep_name`` and
``teatree.loop.tick_freshness._canonical_overlay_names`` historically
encoded the same alias rule in two places and DIVERGED on suffix
matches without a leading dash (the loop variant used
``endswith((f"-{alias}", alias))`` which over-matched). The unified
resolver ``teatree.config.discovery._match_canonical_ep`` is the single home for
the rule; both call sites consume it.
"""

from pathlib import Path
from unittest.mock import patch

import pytest


def test_match_canonical_ep_exact_match() -> None:
    from teatree.config import _match_canonical_ep  # noqa: PLC0415

    assert _match_canonical_ep("t3-acme", {"t3-acme", "t3-teatree"}) == "t3-acme"


def test_match_canonical_ep_dashed_suffix_match() -> None:
    from teatree.config import _match_canonical_ep  # noqa: PLC0415

    # The short alias ``teatree`` folds into ``t3-teatree`` because the
    # ep ends with ``-teatree``.
    assert _match_canonical_ep("teatree", {"unrelated-ep", "t3-teatree"}) == "t3-teatree"


def test_match_canonical_ep_no_match_returns_none() -> None:
    from teatree.config import _match_canonical_ep  # noqa: PLC0415

    assert _match_canonical_ep("ghost", {"t3-acme", "t3-teatree"}) is None


def test_match_canonical_ep_rejects_dashless_suffix() -> None:
    """Reject ep names ending with the alias but missing the dash separator.

    This is the divergence point #1138 unifies: the loop-freshness variant
    historically accepted ``t3acme`` as a canonical match for alias ``acme`` —
    a semantic collision, not a legacy alias. The unified rule requires the
    dash separator.
    """
    from teatree.config import _match_canonical_ep  # noqa: PLC0415

    assert _match_canonical_ep("acme", {"t3acme"}) is None
    assert _match_canonical_ep("teatree", {"myteatree"}) is None


def test_match_canonical_ep_empty_ep_set() -> None:
    from teatree.config import _match_canonical_ep  # noqa: PLC0415

    assert _match_canonical_ep("teatree", set()) is None


def test_canonical_overlay_names_uses_unified_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inherit the strict dash-suffix rule through the unified resolver.

    The loop-freshness ``_canonical_overlay_names()`` now consumes the unified
    resolver, so a ``[overlays.acme]`` table no longer folds into a non-dashed
    ``t3acme`` entry-point (this was the divergence #1138 closes).
    """
    from teatree.loop.tick_freshness import _canonical_overlay_names  # noqa: PLC0415

    toml_path = tmp_path / ".teatree.toml"
    toml_path.write_text(
        "[overlays.teatree]\n[overlays.acme]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    overlays = {"t3-teatree": object(), "t3acme": object()}
    with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
        mapping = _canonical_overlay_names()

    # teatree folds into t3-teatree (dash-suffix match).
    assert mapping.get("teatree") == "t3-teatree"
    # acme does NOT fold into t3acme (no dash separator).
    assert "acme" not in mapping


def test_canonical_overlay_names_folds_dashed_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve the legitimate ``<short>`` → ``t3-<short>`` fold post-unification."""
    from teatree.loop.tick_freshness import _canonical_overlay_names  # noqa: PLC0415

    toml_path = tmp_path / ".teatree.toml"
    toml_path.write_text(
        "[overlays.teatree]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    overlays = {"t3-teatree": object()}
    with patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays):
        mapping = _canonical_overlay_names()
    assert mapping == {"teatree": "t3-teatree"}


def test_no_residual_canonical_ep_name_symbol() -> None:
    """Confirm the pre-#1138 ``_canonical_ep_name`` shim is gone from config."""
    import teatree.config as cfg  # noqa: PLC0415

    assert not hasattr(cfg, "_canonical_ep_name")
