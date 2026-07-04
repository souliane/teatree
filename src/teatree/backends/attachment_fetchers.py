"""Register the per-source attachment downloaders into core (PR-15, M5).

The intake attachment manifest (``teatree.core.attachment_manifest``) resolves a
per-kind fetcher from the core registry; the transport lives here in the backends
layer. ``BackendsConfig.ready`` calls :func:`install_attachment_fetchers` so the
downloaders are available to ``ticket attachments --fetch`` — the same
register-at-ready inversion the reaction publisher uses (core never imports
backends).

Coverage is narrow: ``download_notion_file`` auto-downloads a **signed**
``file.notion.so`` file URL (or a resolvable ``NotionFileRef``). A bare notion
*page* URL (``www.notion.so/…``) is unsigned and not a file ref, so it raises
``ValueError`` and — like GitLab-upload and Slack-file refs, which have no
registered transport at all — falls to ``--fetch``'s manual-placement hint.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from teatree.backends import notion
from teatree.core.attachment_fetch_registry import register_attachment_fetcher
from teatree.core.attachment_manifest import AttachmentKind

if TYPE_CHECKING:
    from teatree.core.attachment_manifest import AttachmentRef


def _fetch_notion(ref: "AttachmentRef", dest: Path) -> Path:
    # Downloads a signed file.notion.so URL only; an unsigned www.notion.so page
    # URL raises ValueError, which the gate reports as a manual-placement hint.
    return notion.download_notion_file(url=ref.source_url, dest=dest)


def install_attachment_fetchers() -> None:
    """Register every wired per-source attachment downloader into core."""
    register_attachment_fetcher(AttachmentKind.NOTION, _fetch_notion)
