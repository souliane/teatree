"""Unique UTC-stamped backup names shared by the facade, sealed store and password-store copy."""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from secrets import token_hex

BACKUP_MARKER = ".bak-"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
BACKUPS_KEPT = 3
BACKUP_MAX_AGE = timedelta(days=90)
_BACKUP_NONCE_HEX_LENGTH = 16


def backup_name(key: str, at: datetime) -> str:
    return f"{key}{BACKUP_MARKER}{at.strftime(STAMP_FORMAT)}-{token_hex(8)}"


def backup_base(name: str) -> str:
    return name.partition(BACKUP_MARKER)[0]


def is_backup(name: str) -> bool:
    return BACKUP_MARKER in name


def backup_stamp(name: str) -> datetime | None:
    _, marker, tail = name.partition(BACKUP_MARKER)
    if not marker:
        return None
    stamp, separator, nonce = tail.partition("-")
    if separator and (
        len(nonce) != _BACKUP_NONCE_HEX_LENGTH or any(character not in "0123456789abcdef" for character in nonce)
    ):
        return None
    try:
        return datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def expired_backup_names(names: Iterable[str], now: datetime) -> frozenset[str]:
    by_base: dict[str, list[tuple[datetime, str]]] = {}
    for name in dict.fromkeys(names):
        if stamp := backup_stamp(name):
            by_base.setdefault(backup_base(name), []).append((stamp, name))
    cutoff = now - BACKUP_MAX_AGE
    return frozenset(
        name
        for dated in by_base.values()
        for rank, (stamp, name) in enumerate(sorted(dated, reverse=True))
        if rank >= BACKUPS_KEPT or stamp < cutoff
    )
