"""The per-source attachment fetcher registry (PR-15, M5).

Backends registers a downloader per kind; core resolves it. An unregistered kind
resolves to ``None`` so the caller raises an actionable manual-download error.
"""

from pathlib import Path

from teatree.core.attachment_fetch_registry import register_attachment_fetcher, resolve_attachment_fetcher


def _noop(source_url: str, dest: Path) -> Path:
    return dest


class TestAttachmentFetchRegistry:
    def test_register_then_resolve_round_trips(self) -> None:
        register_attachment_fetcher("registry-probe-kind", _noop)
        assert resolve_attachment_fetcher("registry-probe-kind") is _noop

    def test_unregistered_kind_resolves_none(self) -> None:
        assert resolve_attachment_fetcher("registry-never-registered-kind") is None
