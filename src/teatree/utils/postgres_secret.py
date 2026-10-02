"""Resolve and persist the per-worktree Postgres password without leaking it.

Privacy-preserving alternative to keeping ``POSTGRES_PASSWORD=<literal>`` in
``.t3-env.cache``.  The cache instead stores ``POSTGRES_PASSWORD_PASS_KEY``,
a symbolic reference (e.g. ``teatree/wt/123/postgres``) that runtime tooling
resolves on demand via the ``pass`` password store.

Resolution order in :func:`resolve_postgres_password`:

1. ``POSTGRES_PASSWORD_PASS_KEY`` env → ``pass show <key>``.
2. ``T3_SECRET_RESOLVER`` env → run that command with the pass key (or
    ``POSTGRES_PASSWORD`` if no pass key is set) as the first argument; its
    stdout (first line, stripped) is the secret.  Used by environments that
    cannot install ``pass``.
3. ``POSTGRES_PASSWORD`` env literal — legacy, raises ``DeprecationWarning``
    once per process so existing setups keep working but get nudged toward
    the symbolic form.

The literal secret never appears in log lines or exception messages
emitted by this module — callers that need to verify resolution should
check ``bool(value)`` or ``len(value)``, not echo the secret itself.
"""

import logging
import os
import warnings
from pathlib import Path

from teatree.utils import secrets
from teatree.utils.run import CommandFailedError, run_checked

logger = logging.getLogger(__name__)

POSTGRES_PASSWORD_ENV = "POSTGRES_PASSWORD"  # noqa: S105 — env-var name, not a secret
PASS_KEY_ENV = "POSTGRES_PASSWORD_PASS_KEY"  # noqa: S105 — env-var name, not a secret
RESOLVER_ENV = "T3_SECRET_RESOLVER"
PASS_KEY_PREFIX = "teatree/wt"  # noqa: S105 — pass key namespace, not a secret
PASS_KEY_SUFFIX = "postgres"  # noqa: S105 — pass key leaf, not a secret

_LITERAL_DEPRECATION_WARNED = False


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
        # Fall through — the symbolic ref existed but resolution failed.
        # The next resolver / literal may still succeed.
        logger.warning("POSTGRES_PASSWORD_PASS_KEY=%s resolved to empty value", pass_key)

    if resolver := source.get(RESOLVER_ENV, "").strip():
        value = _resolve_via_command(resolver, source.get(PASS_KEY_ENV, ""))
        if value:
            return value

    if literal := source.get(POSTGRES_PASSWORD_ENV, ""):
        _warn_literal_password_deprecated()
        return literal

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


def _warn_literal_password_deprecated() -> None:
    global _LITERAL_DEPRECATION_WARNED  # noqa: PLW0603 — module-level flag is the right scope.
    if _LITERAL_DEPRECATION_WARNED:
        return
    _LITERAL_DEPRECATION_WARNED = True
    warnings.warn(
        "POSTGRES_PASSWORD literal is deprecated — store the secret in `pass` "
        "and reference it via POSTGRES_PASSWORD_PASS_KEY. "
        "Run `t3 <overlay> env migrate-secrets` to migrate existing worktrees.",
        DeprecationWarning,
        stacklevel=3,
    )


def _reset_literal_deprecation_state() -> None:
    """Test hook — reset the one-shot deprecation warning flag."""
    global _LITERAL_DEPRECATION_WARNED  # noqa: PLW0603 — test-only reset of a module flag.
    _LITERAL_DEPRECATION_WARNED = False


def ensure_postgres_pass_entry(ticket_id: object, password: str) -> str:
    """Store *password* under the worktree's canonical pass key.

    Returns the pass key on success.  Raises ``PostgresPasswordUnavailableError``
    when ``pass`` is not available — the caller decides whether to fall back
    to the legacy literal-in-cache behavior or fail.
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


def extract_literal_from_cache(cache_path: Path) -> str:
    """Return the literal ``POSTGRES_PASSWORD`` value stored in *cache_path*.

    Returns the empty string when the file is missing, unreadable, or has
    no literal entry (already migrated, or never used the literal form).
    Surfaces only enough information for the migration command — never
    logs the value.
    """
    if not cache_path.is_file():
        return ""
    try:
        body = cache_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if line.startswith(f"{POSTGRES_PASSWORD_ENV}="):
            return line.split("=", maxsplit=1)[1]
    return ""


__all__ = [
    "PASS_KEY_ENV",
    "POSTGRES_PASSWORD_ENV",
    "RESOLVER_ENV",
    "PostgresPasswordUnavailableError",
    "ensure_postgres_pass_entry",
    "extract_literal_from_cache",
    "postgres_pass_key",
    "remove_postgres_pass_entry",
    "resolve_postgres_password",
]
