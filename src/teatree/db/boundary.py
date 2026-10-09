"""One read-write domain for the canonical control database.

SQLite's WAL mode coordinates its writers through an mmap'd ``-shm`` file. mmap
coherence between a host process and a process inside a VM is NOT guaranteed
across Docker Desktop's shared-folder layer (virtiofs / gRPC-FUSE), so two
writers on opposite sides of that boundary can each believe a page is free and
allocate it independently. That is a documented SQLite constraint, not a bug in
anything here, and this install has already taken the resulting damage once:
``PRAGMA integrity_check`` on ``db.sqlite3.precorrupt-20260727T221445`` reports 35
``2nd reference to page N`` cross-links, all inside ``teatree_outbound_claim`` —
the one table the containerized ``t3 review post-comment --live`` path writes.

This is now the SECOND line of defence, not the first. The canonical control DB
lives in the ``teatree_control_db`` named volume (``deploy/docker-compose.yml``),
which has no host path at all, so a host process cannot open it and this rule has
nothing to arbitrate there. What it still covers is every database that IS shared
across the boundary — a per-worktree isolated DB under the bind-mounted
``teatree-worktrees`` root, or any install whose control DB has not been migrated
into the volume yet.

Ownership is a claim FILE beside the database, written by the first containerized
connection. Deliberately sticky — there is no TTL, because a TTL reopens exactly
the race it closes (an idle container, a host write, the container waking up).
Releasing ownership is an operator act, and :meth:`ControlDbBoundary.refusal`
says how.

The claim's limit is what motivated the volume: :attr:`read_write_allowed` is
consulted once, when a connection is BUILT. A process that connected before the
claim appeared keeps writing for its whole life, and no later claim revokes it —
which is why ``t3 doctor`` no longer asks whether a claim file exists but whether
the database is reachable from the host at all (:mod:`teatree.db.write_domain`).

An install that never runs the containerized stack never grows a claim file, so
this module is silent there rather than advisory — it costs one ``stat`` per
connection and changes nothing.
"""

import json
import os
import socket
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from teatree.docker.workflow import is_running_in_container
from teatree.paths import control_db_dir

CLAIM_FILENAME = ".db-owner-container"

_NON_FILE_NAMES = frozenset({"", ":memory:"})


def control_db_unreachable_reason(db_path: Path, *, env: Mapping[str, str]) -> str | None:
    """Why a process aimed at *db_path* cannot reach it, or ``None`` when it can.

    A TOPOLOGY read — a statement about WHERE this code is running, taken before any
    DB work rather than inferred from an exception afterwards. It belongs beside
    :class:`DbBoundaryError` because it answers the same question one rung earlier:
    the boundary class arbitrates a database both domains CAN open, and this
    predicate covers the case where the host cannot open it at all.

    :data:`teatree.paths.DEFAULT_CONTROL_DB_DIR` is a container-only mount BY DESIGN —
    its own docstring calls the unreachability "the enforcement", so a host process
    fails loudly instead of quietly opening a second database. Asking up front is
    what lets a host caller report itself as not-run instead of dying on a raw
    ``OperationalError``, which is neither a :class:`DbBoundaryError` nor anything a
    ``DatabaseError`` handler classifies correctly.

    *db_path* is the database the CALLER is asking about, which is what keeps the
    answer honest for both kinds of caller: a lane asking about the canonical control
    DB passes it, while a command asking about the DB it was configured with passes
    that. Anything living outside :func:`~teatree.paths.control_db_dir` — a test
    database, a per-worktree isolated copy, an in-memory one — is a private database
    with no container-only mount in front of it, so it is reachable and there is
    nothing to report.
    """
    directory = control_db_dir(env)
    if db_path.parent != directory:
        return None
    if directory.is_dir():
        return None
    return (
        f"the canonical control DB directory {directory} does not exist on this host — it is a "
        f"container-only mount by design, so run this in the container (`deploy/t3 ...`)"
    )


class DbBoundaryError(RuntimeError):
    """A host process tried to write a database the containerized stack owns.

    Deliberately NOT a :class:`django.db.DatabaseError`: this is a topology
    fault, not a data condition, and the many ``except DatabaseError`` handlers
    that fail open around the codebase must not swallow it.
    """


class ControlDbBoundary:
    """Which coherence domain may open *db_path* read-write.

    *containerized* is injectable so the rule can be tested from both sides of a
    boundary that a test process cannot actually cross.
    """

    def __init__(self, db_path: Path | str, *, containerized: bool | None = None) -> None:
        # The RAW name is kept alongside the path because ``Path("")`` normalises to
        # ``.``, which would read as a real directory and lose the empty-name case.
        self.name = str(db_path)
        self.db_path = Path(db_path)
        self.containerized = is_running_in_container() if containerized is None else containerized

    @property
    def file_backed(self) -> bool:
        """Whether the database name is a real file rather than an in-memory or URI database.

        ``:memory:``, the empty name, and Django's shared-cache in-memory URI
        (``file:memorydb_default?mode=memory&cache=shared``) have no directory to
        carry a claim and cannot be shared across a mount, so the whole rule is
        inapplicable to them — which is what keeps every test run untouched.
        """
        return self.name not in _NON_FILE_NAMES and not self.name.startswith("file:")

    @property
    def claim_path(self) -> Path:
        return self.db_path.parent / CLAIM_FILENAME

    @property
    def claimed_by_container(self) -> bool:
        return self.file_backed and self.claim_path.is_file()

    @property
    def read_write_allowed(self) -> bool:
        """True inside the container, or for any database no container has claimed."""
        return self.containerized or not self.claimed_by_container

    @property
    def readonly_uri(self) -> str:
        """*db_path* as a ``mode=ro`` SQLite URI.

        Built through :meth:`Path.as_uri` so a path holding a URI-special
        character (space, ``%``, ``?``, ``#``) is percent-encoded rather than
        malforming the URI into a silent read-write open — the same construction
        :mod:`teatree.config.cold_db` uses for its read-only handles.
        """
        return f"{self.db_path.absolute().as_uri()}?mode=ro"

    def claim_for_container(self) -> None:
        """Record that the containerized stack owns this database (idempotent, best-effort).

        Called by the guarded backend on every containerized connection, so the
        claim appears the first time the stack touches a database and never needs
        a separate install step in ``deploy/``. Write failures are swallowed: a
        claim that cannot be recorded must degrade to today's behaviour, never
        take the worker down.
        """
        if not self.containerized or not self.file_backed or self.claim_path.exists():
            return
        payload = {
            "claimed_at": datetime.now(tz=UTC).isoformat(),
            "hostname": socket.gethostname(),
            "role": os.environ.get("TEATREE_ROLE", ""),
            "why": (
                "The containerized stack owns this database read-write. Host connections are "
                "downgraded to read-only because WAL's -shm coordination is not coherent across "
                "the VM boundary. Delete this file only after the stack no longer mounts this "
                "directory; see teatree.db.boundary."
            ),
        }
        try:
            self.claim_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except OSError:
            return

    def refusal(self) -> str:
        """The message a refused host write carries — a remedy, not a diagnosis."""
        return (
            f"{self.db_path} is owned read-write by the containerized teatree stack "
            f"({self.claim_path.name} claims it), so this host process holds a read-only "
            "connection. Two writers across the Docker VM boundary cross-link SQLite pages: "
            "WAL coordinates writers through an mmap'd -shm file whose coherence the shared-folder "
            "layer does not guarantee. Run the command through the container instead "
            "(deploy/t3 <args>, which the `t3` shell alias already points at). To hand ownership "
            f"back to the host, stop the stack and remove {self.claim_path}."
        )
