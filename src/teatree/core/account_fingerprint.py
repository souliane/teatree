"""The active Claude account fingerprint — pure, Django-free readers (#1916).

The account identity is the ``oauthAccount.accountUuid`` in ``~/.claude.json``.
These readers are the single source of truth for that value and the durable
record of the last-recovered account, kept dependency-free (``json`` +
``pathlib`` only) so the ``SessionStart`` hook can detect a ``/login`` switch on
its hot path without importing Django or building backends.

The full detect-invalidate-reprobe recovery cycle (which needs Django and the
network) lives in :mod:`teatree.core.account_switch`, which re-exports these
readers; :mod:`teatree.loop.watchdog` builds its ``AccountState`` from
:func:`current_account_identity`. No other module parses ``~/.claude.json``'s
account identity.
"""

import json
from dataclasses import dataclass
from pathlib import Path

ACTIVE_ACCOUNT_FILE = ".claude.json"
CLAUDE_HOME_DIR = ".claude"
RECOVERED_FINGERPRINT_FILE = "teatree-account-switch.json"


@dataclass(frozen=True, slots=True)
class AccountIdentity:
    """The active Claude Code account — its fingerprint and display email."""

    account_uuid: str
    email: str = ""


def current_account_identity(*, home: Path | None = None) -> AccountIdentity | None:
    """Parse ``~/.claude.json`` once and return the active account, ``None`` when unknown.

    The single parser of the account identity. A missing or malformed file, or
    a record with no ``accountUuid``, is "no signal" (``None``), never an error.
    The ``email`` is the display address, defaulting to ``""`` when absent.
    """
    home = home if home is not None else Path.home()
    cfg = home / ACTIVE_ACCOUNT_FILE
    if not cfg.is_file():
        return None
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    uuid = oauth.get("accountUuid")
    if not isinstance(uuid, str) or not uuid:
        return None
    email = oauth.get("emailAddress", "")
    return AccountIdentity(account_uuid=uuid, email=email if isinstance(email, str) else "")


def current_account_fingerprint(*, home: Path | None = None) -> str:
    """The active account's ``oauthAccount.accountUuid``, ``""`` when unknown.

    A missing or malformed file is "no signal" (``""``), never an error — the
    caller treats an empty fingerprint as "cannot tell" and never claims a
    switch on it.
    """
    identity = current_account_identity(home=home)
    return identity.account_uuid if identity is not None else ""


def _recovered_path(home: Path) -> Path:
    return home / CLAUDE_HOME_DIR / RECOVERED_FINGERPRINT_FILE


def load_recorded_fingerprint(*, home: Path | None = None) -> str:
    """The fingerprint recorded at the last recovery, ``""`` when none."""
    home = home if home is not None else Path.home()
    path = _recovered_path(home)
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return ""
    value = data.get("accountUuid") if isinstance(data, dict) else None
    return value if isinstance(value, str) else ""


def record_fingerprint(fingerprint: str, *, home: Path | None = None) -> Path:
    """Persist *fingerprint* as the last-recovered account (idempotent overwrite)."""
    home = home if home is not None else Path.home()
    path = _recovered_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"accountUuid": fingerprint}), encoding="utf-8")
    return path


def fingerprint_switched(*, home: Path | None = None) -> bool:
    """True only when a previously-recorded account differs from the active one.

    Both fingerprints must be non-empty: an empty active fingerprint ("cannot
    tell") or no prior record (first run) is never a switch. Pure-read — does
    not record or recover; the caller decides what to do on a True.
    """
    home = home if home is not None else Path.home()
    current = current_account_fingerprint(home=home)
    previous = load_recorded_fingerprint(home=home)
    return bool(current) and bool(previous) and current != previous


__all__ = [
    "AccountIdentity",
    "current_account_fingerprint",
    "current_account_identity",
    "fingerprint_switched",
    "load_recorded_fingerprint",
    "record_fingerprint",
]
