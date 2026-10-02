"""Which checkout a doctor check is about — answered by declaration or install, never by cwd.

``Path.cwd()`` cannot answer this question in the venue the answer is needed. ``t3`` runs
inside a container that starts at the image's WORKDIR, which is not a checkout and is never
the directory the operator stood in — :mod:`teatree.core.invocation_cwd` exists precisely
because the host cwd does not survive that boundary, and nothing in ``src/`` ever chdirs away
from it. A check anchored on the cwd therefore resolves NO repository there, and a check that
resolves no repository reports nothing while returning success: a permanent no-op that its own
tests score as passing whenever they chdir into a checkout first (#4727).

Two bearings do survive the boundary, tried in this order. The DECLARED cwd wins where there
is one: an operator standing in a worktree is asking about that checkout, and ``deploy/t3``
translates it into container coordinates for exactly this kind of reader. Otherwise the
installed clone — the tree this very process was imported from — which is what the deployed
container, where nothing is declared, always resolves to.

Neither bearing is assumed to BE a checkout; each is put to ``git`` and only an answer counts.
"""

from pathlib import Path


def installed_clone_root() -> Path:
    """The clone this process was imported from (``<repo>/src/teatree/__init__.py`` → ``<repo>``)."""
    import teatree  # noqa: PLC0415 — deferred: keeps CLI startup light

    return Path(teatree.__file__).resolve().parents[2]


def doctor_checkout_root() -> Path | None:
    """The checkout to inspect, or ``None`` when neither bearing names one."""
    from teatree.core.invocation_cwd import declared_invocation_cwd  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.utils.git_run import run  # noqa: PLC0415 — deferred: keeps CLI startup light

    for candidate in (declared_invocation_cwd(), installed_clone_root()):
        if candidate is None:
            continue
        top = run(repo=str(candidate), args=["rev-parse", "--show-toplevel"])
        if top:
            return Path(top)
    return None


__all__ = ["doctor_checkout_root", "installed_clone_root"]
