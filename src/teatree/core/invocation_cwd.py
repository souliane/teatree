"""Where the operator STOOD when they invoked ``t3`` — a leaf module, no teatree deps.

``deploy/t3`` runs the CLI inside a container via ``docker compose exec``, which
starts the process in the image's own WORKDIR (``/home/teatree``). The host cwd is
never propagated, so inside the container ``Path.cwd()`` is the same directory no
matter where the operator stands. For most commands that is harmless — they take
their bearings from config, the DB, or an explicit argument. It is fatal for a
command whose entire contract IS "resolve from where I am": ``t3 overlay install``
walks up from cwd to find the teatree workspace, and failed identically whether it
was run from the fork root or from ``/tmp``, which is what proved cwd was being
ignored rather than mis-resolved.

**Why an environment variable and not ``docker compose exec --workdir``.** A
``--workdir`` must name a path that exists INSIDE the container, and the host cwd
usually has no container counterpart — only the bind-mounted working tree is
reachable there. Passing the host cwd unconditionally makes docker fail the exec
outright ("no such file or directory") for every invocation from ``~``, ``/tmp``,
or any repo outside the mount, i.e. it breaks every other containerized command to
serve this one. Passing it only when it happens to be mappable is worse: the CLI
would then run under two different cwds depending on where the operator stood,
silently changing behaviour for every cwd-sensitive code path at once (config
discovery, git-repo detection, relative path arguments) with no test covering the
split.

So the cwd crosses the boundary as DATA that a caller opts into reading.
``deploy/t3`` translates the host cwd into container coordinates — it is the only
layer that knows the mount mapping — and exports it here. Unset (the operator
stands outside the mounted tree, or no working tree is mounted at all) the value
degrades to ``Path.cwd()``, which is exactly the pre-existing behaviour.

The general rule this encodes: **a path read on one side of the container boundary
is meaningless on the other unless something explicitly translates it.** Teatree
already hit that with the DB ``overlays`` registry, whose stored ``~``-rooted path
resolves under the operator's home on the host and under ``/home/teatree`` in the
container (``config.discovery._registry_project_path``). This module is the same
lesson for the invocation cwd.
"""

import logging
import os
from pathlib import Path

#: Set by ``deploy/t3`` to the CONTAINER-side path of the operator's host cwd.
#: Never a host path — translating is the wrapper's job, not the reader's.
INVOCATION_CWD_ENV = "TEATREE_INVOCATION_CWD"

_log = logging.getLogger(__name__)


def declared_invocation_cwd() -> Path | None:
    """The declared container-side cwd, or ``None`` when nothing usable was declared.

    Split out from :func:`invocation_cwd` because the two callers need different
    fallbacks. ``invocation_cwd`` wants ``Path.cwd()``; a caller with its own
    richer chain (``resolve.intake``'s ``T3_ORIG_CWD`` → ``PWD`` → ``Path.cwd()``)
    must be able to tell "nothing was declared" from "the declaration IS the
    process cwd", and can only do that if the absence is representable.

    A declared path that does not resolve to a directory here is ignored rather
    than trusted — a stale or untranslated value must not silently redirect a
    command to a path that means something different on this side. Discarding it
    is logged, because the fallback is exactly the pre-#776 process cwd whose
    wrongness this module exists to fix, and the refusals that tell operators to
    export this variable are unreadable if a bad value discards without a word.
    """
    declared = os.environ.get(INVOCATION_CWD_ENV, "").strip()
    if not declared:
        return None
    candidate = Path(declared)
    if candidate.is_dir():
        return candidate
    _log.warning(
        "%s=%r is not a directory in this runtime — ignoring it and falling back to the process cwd. "
        "It must name the CONTAINER-side path of your checkout, not the host path.",
        INVOCATION_CWD_ENV,
        declared,
    )
    return None


def invocation_cwd() -> Path:
    """The directory the operator invoked ``t3`` from, in THIS runtime's coordinates.

    Falls back to :func:`Path.cwd` when nothing declared one, so a host-native run
    and a containerized run from outside the mounted tree both behave as before.
    """
    return declared_invocation_cwd() or Path.cwd()


__all__ = ["INVOCATION_CWD_ENV", "declared_invocation_cwd", "invocation_cwd"]
