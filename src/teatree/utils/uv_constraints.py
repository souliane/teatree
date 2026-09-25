"""The ``--constraints`` argument every teatree-owned ``uv tool install`` must carry.

The deployed image exports ``UV_CONSTRAINT`` as a build-time expansion of
``$TEATREE_CLONE_DIR``, so a layout that vendors core under ``vendor/teatree`` — and
overrides that variable at RUNTIME — inherits an ambient value naming a path nothing ever
writes. ``uv tool install`` errors outright on a missing constraints path, so every install
that relies on the ambient value dies there (souliane/teatree#4659).

``deploy/entrypoint.sh`` realigns the ambient value for its own role's process tree, but a
``docker exec``-ed process starts from the container's create-time environment and never sees
that export. An explicit flag is the only bound that travels every venue: it overrides the
ambient value and it follows the checkout being installed, so it is correct on a standalone
clone and a vendored subtree alike. This is the ``--constraints`` sibling of
:mod:`teatree.utils.uv_overrides`, which exists for the same reason.
"""

import shlex
from pathlib import Path

#: Generated from the checkout's own ``uv.lock`` at boot and gitignored — never committed.
UV_CONSTRAINTS_FILENAME = "uv-constraints.txt"


def uv_constraints_args(checkout: Path) -> list[str]:
    """``["--constraints", "<checkout>/uv-constraints.txt"]`` — empty when the file is absent.

    The file is generated, so a dev checkout that has never booted a container legitimately
    has none. It degrades to no flag rather than naming a path that does not exist, which
    would turn a recoverable reinstall into the hard resolver error this module prevents.
    """
    constraints = checkout / UV_CONSTRAINTS_FILENAME
    return ["--constraints", str(constraints)] if constraints.is_file() else []


def uv_tool_install_hint(command: str, checkout: Path | None = None) -> str:
    source = checkout if checkout is not None else Path(__file__).resolve().parents[3]
    constraints = uv_constraints_args(source)
    return f"{command} {shlex.join(constraints)}" if constraints else command
