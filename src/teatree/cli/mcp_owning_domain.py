"""Run ``t3 mcp serve`` in the domain that owns the control database.

The MCP server is a WRITER — ``review_post_comment``, ``ticket_visit_phase``,
``task_create`` and their siblings all persist through the ORM. When the
containerized stack owns the control DB (:mod:`teatree.db.boundary`), a host
server's writes are refused, so the server has to run where the writes are legal.

A shell alias cannot reach it: ``t3 setup`` aliases ``t3`` to ``deploy/t3`` for
interactive shells, but Claude Code launches the server from ``.mcp.json`` as a
PATH lookup, which resolves the native console script and bypasses the alias
entirely. That is how four host ``t3 mcp serve`` processes came to hold the
bind-mounted database read-write while the containerized stack wrote it too.

So the routing lives here instead of in ``.mcp.json``: the same decision the
alias encodes, taken at startup from the observed ownership of the database
rather than pinned into a committed file. An install whose database no
container has claimed serves natively and never touches Docker — which is every
plain teatree clone.
"""

import os
from pathlib import Path

from teatree.cli.setup.clone import find_main_clone
from teatree.db.boundary import ControlDbBoundary, control_db_unreachable_reason
from teatree.docker.workflow import is_running_in_container, wrapper_path
from teatree.paths import CANONICAL_DB

DELEGATED_ENV_VAR = "T3_MCP_DELEGATED"


def owning_domain_wrapper() -> Path | None:
    """The container-wrapping ``t3`` entry to hand this server to, or ``None`` to serve here.

    ``None`` — serve natively — whenever anything is uncertain: already inside the
    container, a database no container has claimed, an unresolvable clone, a missing
    or non-executable wrapper, or a delegation that already happened. Failing to
    delegate is safe: the boundary guard still refuses the write, loudly and with the
    remedy. Delegating on a bad guess is not.
    """
    if is_running_in_container() or os.environ.get(DELEGATED_ENV_VAR):
        return None
    # The line above already settled which side of the boundary this process is on, so
    # the domain is passed down rather than re-derived — one detection, one answer.
    # A DB the host cannot REACH is owned by the container even though no claim file is
    # visible here — the claim lives inside the volume. Asking read_write_allowed alone
    # inverts the guard exactly where it matters.
    if control_db_unreachable_reason(CANONICAL_DB, env=os.environ) is None and (
        ControlDbBoundary(CANONICAL_DB, containerized=False).read_write_allowed
    ):
        return None

    try:
        repo = find_main_clone()
    except Exception:  # noqa: BLE001 — clone resolution reaches the filesystem; any failure means "serve here"
        return None
    if repo is None:
        # ``find_main_clone`` reports an unresolvable clone by RETURNING None, not by
        # raising, so the guard above does not cover it. Serving here is the documented
        # answer for that case; passing None on would raise out of a function whose
        # whole contract is to answer rather than fail.
        return None
    wrapper = wrapper_path(repo)
    return wrapper if wrapper.is_file() and os.access(wrapper, os.X_OK) else None


def delegate_to_owning_domain() -> None:
    """Replace this process with the containerized server when the container owns the DB.

    ``execv``, not a subprocess: the client talks to this process over inherited
    stdin/stdout, and replacing the image hands it those exact file descriptors with
    no relaying layer to buffer or drop a frame. Returns normally — serve here —
    whenever :func:`owning_domain_wrapper` declines.
    """
    wrapper = owning_domain_wrapper()
    if wrapper is None:
        return
    os.environ[DELEGATED_ENV_VAR] = "1"
    os.execv(str(wrapper), [str(wrapper), "mcp", "serve"])  # noqa: S606 — argv list, no shell; path from the clone
