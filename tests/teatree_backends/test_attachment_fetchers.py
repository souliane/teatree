"""Backends registers the Notion attachment downloader into core (PR-15, M5)."""

import tempfile
from pathlib import Path
from unittest import mock

import httpx
import pytest

import teatree.backends.notion as notion_mod
from teatree.backends.attachment_fetchers import install_attachment_fetchers
from teatree.core.intake.attachment_fetch_registry import resolve_attachment_fetcher
from teatree.core.intake.attachment_manifest import AttachmentKind, AttachmentRef


class TestInstallAttachmentFetchers:
    def test_registers_a_notion_fetcher_that_downloads(self) -> None:
        install_attachment_fetchers()
        fetcher = resolve_attachment_fetcher(AttachmentKind.NOTION)
        assert fetcher is not None

        captured: dict[str, object] = {}

        def _fake_download(*, url: str, dest: Path) -> Path:
            captured["url"] = url
            dest.write_bytes(b"notion-bytes")
            return dest

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(notion_mod, "download_notion_file", _fake_download),
        ):
            out = fetcher(AttachmentRef("https://www.notion.so/p", AttachmentKind.NOTION), Path(tmp) / "f.png")
            assert captured["url"] == "https://www.notion.so/p"
            assert out.exists()

    def test_notion_fetcher_rejects_a_bare_page_url(self) -> None:
        """A bare www.notion.so page URL is unsigned, so the fetcher rejects it.

        The narrowed coverage claim: only signed file.notion.so URLs auto-download;
        a page URL raises ValueError, so the gate falls to the manual-placement hint.
        """
        install_attachment_fetchers()
        fetcher = resolve_attachment_fetcher(AttachmentKind.NOTION)
        assert fetcher is not None

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(notion_mod, "_brave_cookies", return_value=httpx.Cookies()),
            pytest.raises(ValueError, match="not signed"),
        ):
            fetcher(AttachmentRef("https://www.notion.so/acme/Design-abc", AttachmentKind.NOTION), Path(tmp) / "f")
