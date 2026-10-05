"""Host-qualified private repository entries and their visibility decision."""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from teatree.config import cold_reader


class _VisibilityOps(Protocol):
    """Slug and probe functions supplied by the visibility module."""

    @property
    def slug_for_remote_url(self) -> Callable[[str], str]: ...

    @property
    def forge_qualified_slug(self) -> Callable[[str, str], str]: ...

    @property
    def slug_visibility(self) -> Callable[[str], str | None]: ...

    @property
    def slug_is_allowlisted_private(self) -> Callable[[str, Path | None], bool]: ...


def private_repo_entry_matches(entry: str, remote: str, *, normalize: Callable[[str], str]) -> bool:
    """Match a host-qualified private entry against a canonical remote on segment boundaries."""
    from teatree.config.setting_parsers import _parse_private_repos  # noqa: PLC0415 — cold import

    try:
        canonical_entry = _parse_private_repos([entry])[0]
    except (TypeError, ValueError):
        return False
    canonical_remote = normalize(remote).lower()
    return bool(canonical_remote) and (
        canonical_remote == canonical_entry or canonical_remote.startswith(f"{canonical_entry}/")
    )


_WARNED_MALFORMED_PRIVATE: set[tuple[str, str]] = set()


def reset_malformed_private_warnings() -> None:
    """Clear process-local warning deduplication between isolated runs."""
    _WARNED_MALFORMED_PRIVATE.clear()


def warn_malformed_private_entry(entry: object, config_path: Path | None = None) -> None:
    """Report one warning per malformed stored entry and config store."""
    key = (str(config_path or cold_reader.canonical_config_db()), repr(entry))
    if key in _WARNED_MALFORMED_PRIVATE:
        return
    _WARNED_MALFORMED_PRIVATE.add(key)
    sys.stderr.write(f"ignoring malformed private_repos entry {entry!r}; expected host/owner or host/owner/repo\n")


def _private_repo_allowlist(config_path: Path | None = None) -> list[str]:
    """Read valid host-qualified private entries; report malformed legacy rows."""
    from teatree.config.setting_parsers import _parse_private_repos  # noqa: PLC0415 — cold import

    raw = cold_reader.list_setting("private_repos", default=[], db_path=config_path)
    entries = []
    for entry in raw:
        try:
            entries.extend(_parse_private_repos([entry]))
        except (TypeError, ValueError):
            warn_malformed_private_entry(entry, config_path)
    return entries


def private_repo_visibility(
    slug: str,
    config_path: Path | None = None,
    *,
    forge: str = "",
    ops: _VisibilityOps,
) -> str | None:
    """Probe first so a reachable PUBLIC answer overrides a private declaration."""
    qualified = qualified_repo_slug(slug, forge, ops=ops)
    if not qualified or "." not in qualified.split("/", 1)[0]:
        return None
    verdict = ops.slug_visibility(qualified)
    if verdict == "PUBLIC":
        return verdict
    if verdict in {"PRIVATE", "INTERNAL"}:
        return verdict
    if ops.slug_is_allowlisted_private(qualified, config_path):
        return "PRIVATE"
    return verdict


def qualified_repo_slug(ref: str, forge: str = "", *, ops: _VisibilityOps) -> str:
    """Preserve a target's URL host and reduce issue/PR URLs to their repo."""
    if ref.startswith(("http://", "https://")):
        from teatree.utils.url_slug import project_slug_from_ref  # noqa: PLC0415 — cold import

        if project := project_slug_from_ref(ref):
            parsed = urlsplit(ref)
            return f"{parsed.hostname}/{project}" if parsed.hostname else ""
    return ops.forge_qualified_slug(ops.slug_for_remote_url(ref), forge)
