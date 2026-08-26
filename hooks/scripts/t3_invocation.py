"""The one seam every hook uses to invoke the ``t3`` CLI.

A hook is a bare ``python3`` subprocess the harness starts in the SESSION's
working directory, and the containerized ``t3`` entry point REFUSES to run when
that directory is a checkout it cannot see from inside the container. A hook that
shells out with an inherited cwd therefore gets a non-zero exit that says nothing
about what it asked — and a gate that fails CLOSED reads that exit as a DENY and
blocks legitimate work. The failure mode is "correct work is refused", not
"something looks odd", so nothing downstream reports it.

Two gates rediscovered that bug independently and one of them fixed it inline, at
its own call site. Two independent rediscoveries of one bug is the signal that the
abstraction is missing rather than that another fix is needed, so this module is
the abstraction: every ``t3`` shell-out in ``hooks/scripts`` resolves its argv
through :func:`t3_argv` and runs it through :func:`run_t3` or
:func:`spawn_t3_detached`, which pin a directory the container can always see —
the checkout this hook package was installed from, derived from this module's own
location and verified against the tree rather than trusted as a constant depth.

Not using the seam is the visible anomaly:
``tests/conformance/test_hook_t3_invocation_seam.py`` fails on a hook that resolves
or spawns ``t3`` on its own, so a hook added tomorrow cannot reintroduce the bug by
omission.

The pinned cwd is safe for every caller because none of them resolve their SUBJECT
from the cwd: each passes it explicitly (``--repo``, a title, a body on stdin). A
caller whose subject genuinely IS a directory passes ``cwd=`` and keeps it.

An unresolvable checkout fails LOUD, never closed and never silent: one stderr line
names the layout that did not hold, and the call proceeds with the inherited
directory rather than being blocked. A gate that cannot locate its own tree must
not become a gate that refuses everything.

Cold-import safe: the live hook is a bare ``python3`` subprocess with no guarantee
``teatree`` is importable, so the module top imports only stdlib.
"""

import shutil
import subprocess  # noqa: S404 — stdlib subprocess for the trusted internal `t3` CLI
import sys
from pathlib import Path

# Alias the bare and ``hooks.scripts.`` identities so a module importing one and a
# test patching the other operate on ONE module object.
sys.modules.setdefault("t3_invocation", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.t3_invocation", sys.modules[__name__])

#: The CLI this seam exists to invoke. The one place the name is written.
T3_BINARY = "t3"

#: Where this module sits inside the checkout: ``<root>/hooks/scripts/<name>.py``.
_PACKAGE_SEGMENTS = ("hooks", "scripts")


def hook_checkout_root() -> Path | None:
    """The checkout this hook package was installed from, or ``None``.

    Derived from this module's own resolved location and then CHECKED against the
    tree — the candidate root must lead back to this exact file — so the depth is
    verified rather than copied. ``None`` means the layout does not hold and no
    container-visible directory can be named.
    """
    module = Path(__file__).resolve()
    root = module.parents[len(_PACKAGE_SEGMENTS)]
    return root if root.joinpath(*_PACKAGE_SEGMENTS, module.name) == module else None


def t3_invocation_cwd() -> str | None:
    """A directory the containerized ``t3`` can always see, or ``None`` — announced.

    ``None`` degrades to the inherited session directory, which is what the
    containerized entry point may refuse; the stderr line is what keeps that
    degradation from being silent.
    """
    root = hook_checkout_root()
    if root is not None:
        return str(root)
    sys.stderr.write(
        "NOTE: the t3 hook seam could not locate its own checkout from "
        f"{Path(__file__).resolve()} — a `t3` shell-out will inherit the session "
        "directory, which the containerized entry point refuses when that directory "
        "is not visible inside the container. Reinstall the hooks from a checkout "
        "laid out as <root>/hooks/scripts/.\n"
    )
    return None


def t3_available() -> bool:
    """Whether the ``t3`` CLI is on this hook's (restricted) PATH."""
    return shutil.which(T3_BINARY) is not None


def t3_argv(*args: str) -> list[str] | None:
    """The argv for ``t3 <args>``, or ``None`` when ``t3`` is not on PATH.

    The single resolver: no other hook module looks ``t3`` up itself, which is what
    lets the conformance test find a call site that skipped the seam.
    """
    binary = shutil.which(T3_BINARY)
    return [binary, *args] if binary else None


def run_t3(
    argv: list[str],
    *,
    timeout: int,
    cwd: str | Path | None = None,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *argv* to completion from a container-visible directory.

    The house posture for a hook shell-out, fixed here so it cannot drift per call
    site: captured text output, no ``check`` (the caller reads the exit code and
    classifies it), and a mandatory *timeout* so no hook outlives its budget.

    *cwd* overrides the pinned default for a caller whose subject IS a directory.
    Omitting it is what makes the session directory unreachable.
    """
    return subprocess.run(  # noqa: S603 — trusted internal subprocess; fixed argv, no shell
        argv,
        input=stdin_text,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        cwd=str(cwd) if cwd is not None else t3_invocation_cwd(),
    )


def spawn_t3_detached(argv: list[str]) -> None:
    """Fire *argv* and forget it — a detached, best-effort ``t3`` call.

    Its own session and null streams, so slow work never holds the hook open. The
    cwd is pinned for the same reason as :func:`run_t3`: an inherited session
    directory turns the spawn into a refusal nobody ever sees.
    """
    subprocess.Popen(  # noqa: S603 — detached, fire-and-forget; trusted internal CLI
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=t3_invocation_cwd(),
    )
