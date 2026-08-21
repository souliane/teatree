"""Pins the Django version window so widening it can never be an invisible one-liner (#4437).

Nothing guarded the ceiling. Widening ``django>=6,<6.1`` to ``<6.2`` resolves to
the SAME 6.0.7 in isolation — ``uv`` preserves locked versions — so the diff
reads as a no-op and CI has nothing new to test. What it actually does is
authorise the *next* automated resolve: measured at the time of #4432, the
weekly ``uv lock --upgrade`` takes 6.0.8 (the in-series patch) under ``<6.1``
and 6.1 (a new feature series) under ``<6.2``.

So this file is a ratchet, not a style check: the pinned constants below make a
one-character edit RED and name #4404 — which owns the deliberate Python 3.14 +
Django 6.1 move, including its branch-protection ordering — as the only place an
intentional change is decided. Moving the pin here without moving it there is
the failure mode.

The window invariant is the second half: a pin is only safe if it admits exactly
ONE feature series, so ``>=6,<6.2`` stays RED even after #4404 bumps the
constants to the next series.
"""

import re
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

PINNED_DJANGO_FLOOR = (6, 0)
PINNED_DJANGO_CEILING = (6, 1)
CEILING_OWNER = "https://github.com/souliane/teatree/issues/4404"

_OWNER_HINT = (
    f"Any intentional change to the Django window is owned by {CEILING_OWNER} "
    "(the deliberate Python 3.14 + Django 6.1 move, including branch-protection "
    "ordering). Update the pin there, with that issue, not in a dependency bump."
)


def _django_specifier() -> str:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    deps: list[str] = data["project"]["dependencies"]
    return next(dep for dep in deps if re.match(r"^django\b(?!-)", dep))


def _bound(specifier: str, operator: str) -> tuple[int, int] | None:
    """The ``major.minor`` of one bound, or ``None`` when the pin declares no such bound.

    ``pyproject-fmt`` strips a ``.0`` minor (``django>=6.0`` normalises to
    ``django>=6``), so an absent minor reads as 0 rather than as a parse failure.
    """
    match = re.search(rf"{re.escape(operator)}\s*(\d+)(?:\.(\d+))?", specifier)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


class TestDjangoVersionWindow:
    def test_pin_declares_an_explicit_upper_bound(self) -> None:
        specifier = _django_specifier()
        assert _bound(specifier, "<") is not None, (
            f"The Django pin ({specifier!r}) declares no '<' upper bound, so "
            "`uv lock --upgrade` may resolve to any future feature series — "
            f"unattended, via the weekly lock refresh. {_OWNER_HINT}"
        )

    def test_ceiling_is_the_pinned_series(self) -> None:
        specifier = _django_specifier()
        ceiling = _bound(specifier, "<")
        expected = f"{PINNED_DJANGO_CEILING[0]}.{PINNED_DJANGO_CEILING[1]}"
        assert ceiling == PINNED_DJANGO_CEILING, (
            f"The Django ceiling moved: pyproject declares {specifier!r} but this "
            f"ratchet pins '<{expected}'. A ceiling widen is invisible in isolation "
            "(the resolved version does not move) and authorises the next weekly "
            f"`uv lock --upgrade` to cross a feature boundary. {_OWNER_HINT}"
        )

    def test_floor_is_the_pinned_series(self) -> None:
        specifier = _django_specifier()
        floor = _bound(specifier, ">=")
        expected = f"{PINNED_DJANGO_FLOOR[0]}.{PINNED_DJANGO_FLOOR[1]}"
        assert floor == PINNED_DJANGO_FLOOR, (
            f"The Django floor moved: pyproject declares {specifier!r} but this "
            f"ratchet pins '>={expected}'. The floor is what django-upgrade's "
            f"--target-version tracks (tests/test_django_upgrade_hook.py). {_OWNER_HINT}"
        )

    def test_window_admits_exactly_one_feature_series(self) -> None:
        specifier = _django_specifier()
        floor, ceiling = _bound(specifier, ">="), _bound(specifier, "<")
        assert floor is not None
        assert ceiling is not None
        successors = {(floor[0], floor[1] + 1), (floor[0] + 1, 0)}
        assert ceiling in successors, (
            f"The Django window {specifier!r} spans more than one feature series: "
            f"the ceiling must be the series immediately after the floor "
            f"({' or '.join(f'{major}.{minor}' for major, minor in sorted(successors))}). "
            "A multi-series window lets the weekly lock refresh migrate the framework "
            f"with no diff a reviewer can see. {_OWNER_HINT}"
        )
