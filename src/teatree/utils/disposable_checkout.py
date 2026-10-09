"""Whether a checkout exists only for the length of the scratch work it holds (#4577).

A cold review, a merge test, or a scratch clone lives under a temp root and is deleted
when the work ends. A pull-request obligation registered against one can never be
discharged — 16 of them accumulated ~12,000 failed drains and 16 permanent
``t3 doctor check`` FAILs that buried the five real findings underneath.

The signal has to be the PATH, not a flag from the caller: the obligation is written by
``ensure-pr`` running inside the git PRE-push hook, whose caller is whatever ad-hoc
``git push`` an agent ran in a clone it made by hand (the measured leaks name
``/tmp/mt-4413``, ``/tmp/rv4510/repo``, ``/var/tmp/rev-4521``). There is no teatree frame
in that stack to know anything, so "under a temp root" is the whole of what is knowable.

The roots are read from the environment at call time rather than frozen at import, so a
box whose scratch clones live somewhere else can say so — and so the test suite can pin
them away from the ``tmp_path`` its own git fixtures build under.
"""

import os
import tempfile
from pathlib import Path

#: Overrides the defaults entirely (``os.pathsep``-separated), never adds to them.
DISPOSABLE_ROOTS_ENV = "TEATREE_DISPOSABLE_CHECKOUT_ROOTS"

_DEFAULT_ROOTS: tuple[str, ...] = ("/tmp", "/var/tmp")  # noqa: S108 — naming fixed roots, not creating temp files


def disposable_roots() -> tuple[Path, ...]:
    """The roots under which a checkout is treated as scratch, resolved and deduplicated."""
    override = os.environ.get(DISPOSABLE_ROOTS_ENV)
    raw = override.split(os.pathsep) if override is not None else [*_DEFAULT_ROOTS, tempfile.gettempdir()]
    roots: dict[Path, None] = {}
    for entry in raw:
        # A blank segment resolves to the cwd, which would make every path disposable.
        if entry.strip():
            roots[Path(entry).resolve()] = None
    return tuple(roots)


def is_disposable_checkout(path: str | Path) -> bool:
    """Whether *path* names a checkout that exists only until its scratch work ends.

    Both sides are resolved before comparison so a symlinked temp root (``/tmp`` ->
    ``/private/tmp``) still matches, and the comparison is by path COMPONENT, so
    ``/tmpfoo`` is not a child of ``/tmp``. A root itself is not a checkout.
    """
    resolved = Path(path).resolve()
    return any(resolved != root and resolved.is_relative_to(root) for root in disposable_roots())


__all__ = ["DISPOSABLE_ROOTS_ENV", "disposable_roots", "is_disposable_checkout"]
