"""Registry for per-source attachment fetchers (PR-15, M5).

The manifest's ``--fetch`` downloads a GitLab upload / Notion file / Slack file,
but that transport lives in ``teatree.backends`` (the higher layer). Rather than
``core`` importing ``backends``, ``backends`` registers a fetcher per source kind
here at app-ready time and :func:`teatree.core.intake.attachment_manifest.default_fetcher`
resolves it when a fetch runs — the same inversion
:mod:`teatree.core.reaction_dispatch` uses for the Slack reaction publisher.

Fail-actionable: an unregistered kind resolves to ``None`` so the caller raises a
clear "download it manually into <dir>" error rather than silently marking the
attachment fetched.
"""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.core.intake.attachment_manifest import AttachmentRef

AttachmentFetcher = Callable[["AttachmentRef", Path], Path]
"""Download seam: fetch *ref* to the given path, return it, raise on failure."""

_fetchers: dict[str, AttachmentFetcher] = {}


def register_attachment_fetcher(kind: str, fetcher: AttachmentFetcher) -> None:
    """Register the downloader for one source *kind* (idempotent; last write wins)."""
    _fetchers[str(kind)] = fetcher


def resolve_attachment_fetcher(kind: str) -> "AttachmentFetcher | None":
    """The registered fetcher for *kind*, or ``None`` when no transport is wired."""
    return _fetchers.get(str(kind))
