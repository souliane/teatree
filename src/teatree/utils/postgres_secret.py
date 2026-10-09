"""Resolve and persist the per-worktree Postgres password without leaking it.

The cache stores ``POSTGRES_PASSWORD_PASS_KEY``,
a symbolic reference (e.g. ``teatree/wt/123/postgres``) that runtime tooling
resolves on demand via the ``pass`` password store.

Resolution order in :func:`resolve_postgres_password`:

1. ``POSTGRES_PASSWORD_PASS_KEY`` env → ``pass show <key>``.
2. ``T3_SECRET_RESOLVER`` env → run that command with the pass key as the
    first argument; its stdout (first line, stripped) is the secret.

The literal secret never appears in log lines or exception messages
emitted by this module — callers that need to verify resolution should
check ``bool(value)`` or ``len(value)``, not echo the secret itself.
"""

import logging
import os

from teatree.utils import secrets
from teatree.utils.run import CommandFailedError, run_checked

logger = logging.getLogger(__name__)

POSTGRES_PASSWORD_ENV = "POSTGRES_PASSWORD"  # noqa: S105 — env-var name, not a secret
PASS_KEY_ENV = "POSTGRES_PASSWORD_PASS_KEY"  # noqa: S105 — env-var name, not a secret
RESOLVER_ENV = "T3_SECRET_RESOLVER"
PASS_KEY_PREFIX = "teatree/wt"  # noqa: S105 — pass key namespace, not a secret
PASS_KEY_SUFFIX = "postgres"  # noqa: S105 — pass key leaf, not a secret


class PostgresPasswordUnavailableError(RuntimeError):
    """Raised when no resolution strategy can produce a Postgres password."""


def postgres_pass_key(ticket_id: object) -> str:
    """Return the canonical ``pass`` key path for *ticket_id*.

    *ticket_id* is stringified — the caller can pass a ``Ticket.ticket_number``,
    primary key, or arbitrary string identifier.  Empty values yield a
    ``ValueError`` so the caller never silently creates a flat ``teatree/wt//
    postgres`` entry that would collide across worktrees.
    """
    value = str(ticket_id).strip()
    if not value:
        msg = "ticket_id must be non-empty to build a postgres pass-key"
        raise ValueError(msg)
    return f"{PASS_KEY_PREFIX}/{value}/{PASS_KEY_SUFFIX}"


def resolve_postgres_password(env: dict[str, str] | None = None) -> str:
    """Return the Postgres password without storing it anywhere observable.

    Reads from *env* (defaults to ``os.environ``) following the resolution
    order documented at the module docstring.  Returns the empty string
    when no source is available — callers decide whether that's fatal.
    """
    source = env if env is not None else os.environ
    if pass_key := source.get(PASS_KEY_ENV, "").strip():
        value = secrets.read_pass(pass_key)
        if value:
            return value
        # The configured resolver may still have the symbolic reference.
        logger.warning("POSTGRES_PASSWORD_PASS_KEY=%s resolved to empty value", pass_key)

    if pass_key and (resolver := source.get(RESOLVER_ENV, "").strip()):
        value = _resolve_via_command(resolver, pass_key)
        if value:
            return value

    return ""


def _resolve_via_command(command: str, pass_key: str) -> str:
    """Invoke *command* with *pass_key* and return its first stdout line.

    Lets non-``pass`` users supply any resolver that echoes the secret
    (e.g. 1Password CLI, Bitwarden CLI, sops, age).  Failure to invoke
    or non-zero exit returns the empty string.
    """
    parts = command.split()
    if pass_key:
        parts.append(pass_key)
    try:
        result = run_checked(parts)
    except (CommandFailedError, FileNotFoundError):
        logger.warning("T3_SECRET_RESOLVER command failed to resolve postgres password")
        return ""
    line = result.stdout.strip().splitlines()
    return line[0] if line else ""


def ensure_postgres_pass_entry(ticket_id: object, password: str) -> str:
    """Store *password* under the worktree's canonical pass key.

    Returns the pass key on success.  Raises ``PostgresPasswordUnavailableError``
    when ``pass`` is not available.
    """
    if not password:
        msg = "password must be non-empty to be stored in pass"
        raise ValueError(msg)
    key = postgres_pass_key(ticket_id)
    if not secrets.write_pass(key, password):
        msg = (
            "pass is not installed or pass insert failed — cannot store the "
            "postgres password symbolically. Install pass or set "
            "T3_SECRET_RESOLVER to keep secrets out of .t3-env.cache."
        )
        raise PostgresPasswordUnavailableError(msg)
    return key


def remove_postgres_pass_entry(ticket_id: object) -> bool:
    """Remove the worktree's postgres entry from ``pass``.

    Returns ``True`` when ``pass rm`` reported success, ``False`` otherwise
    (no entry, pass not installed, command failed).  Callers should treat
    a ``False`` as best-effort — teardown should not block on it.
    """
    key = postgres_pass_key(ticket_id)
    return secrets.remove_pass(key)


__all__ = [
    "PASS_KEY_ENV",
    "POSTGRES_PASSWORD_ENV",
    "RESOLVER_ENV",
    "PostgresPasswordUnavailableError",
    "ensure_postgres_pass_entry",
    "postgres_pass_key",
    "remove_postgres_pass_entry",
    "resolve_postgres_password",
]
