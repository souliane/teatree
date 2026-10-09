"""Overlay-registry stand-ins shared by the board-reconcile rule lanes."""

import contextlib
from collections.abc import Iterator
from unittest.mock import patch


@contextlib.contextmanager
def rule_f_inert() -> Iterator[None]:
    """Judge no URL, so rule F cannot act in a lane that is about another rule (#4711).

    Rule F's candidates are the PRE-SHIP states most of these fixtures sit in, and it reads
    the live forge — an unregistered overlay is the one thing that provably stops it.
    """
    with patch("teatree.core.overlay_loader.get_all_overlays", return_value={}):
        yield


@contextlib.contextmanager
def overlays_registered(name: str = "t3-teatree") -> Iterator[None]:
    """Register *name* in the overlay registry so the per-URL probe is actually reached."""
    with patch("teatree.core.overlay_loader.get_all_overlays", return_value={name: object()}):
        yield
