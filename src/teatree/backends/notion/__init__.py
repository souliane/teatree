"""Notion access — headless via an integration token, plus the file-CDN path.

:mod:`~teatree.backends.notion.client` is the headless surface an unattended run
uses: pages, blocks, comments, properties and databases over the public API with
an internal integration token from the ``pass`` store.
:mod:`~teatree.backends.notion.sections` adds the write primitive the PRD/BDD
skills need — block-scoped replacement of a single named section;
:mod:`~teatree.backends.notion.comments` posts a page comment at most once per
marker; :mod:`~teatree.backends.notion.properties` reads and writes one page
property. All three verify by re-reading before reporting success.
:mod:`~teatree.backends.notion.attachments` is the exception that is NOT
headless: a file block's bytes need a logged-in browser session.
"""

from teatree.backends.notion.attachments import NotionFileRef, download_notion_file, resolve_signed_url
from teatree.backends.notion.blocks import build_blocks, heading_block, literal_rich_text, rich_text
from teatree.backends.notion.client import NotionClient, NotionTokenCredential, option_name
from teatree.backends.notion.comments import CommentPoster, CommentPostResult, comment_text
from teatree.backends.notion.errors import (
    NotionAmbiguousSectionError,
    NotionBadTokenError,
    NotionCapabilityDeniedError,
    NotionError,
    NotionErrorClassifier,
    NotionNotSharedError,
    NotionObjectNotFoundError,
    NotionPropertyNotFoundError,
    NotionRateLimitedError,
    NotionTokenMissingError,
    NotionUnsupportedMarkdownError,
    NotionUnwritablePropertyError,
    NotionWriteNotLandedError,
    normalize_object_id,
)
from teatree.backends.notion.markdown import BlockMarkdownRenderer, rich_text_to_markdown
from teatree.backends.notion.properties import (
    PagePropertyWriter,
    PropertyWrite,
    PropertyWriteResult,
    build_property_write,
    page_property,
    plain_property_value,
    property_type,
)
from teatree.backends.notion.sections import (
    ResolvedSection,
    SectionLocator,
    SectionWriter,
    SectionWriteResult,
    normalize_heading,
)

__all__ = [
    "BlockMarkdownRenderer",
    "CommentPostResult",
    "CommentPoster",
    "NotionAmbiguousSectionError",
    "NotionBadTokenError",
    "NotionCapabilityDeniedError",
    "NotionClient",
    "NotionError",
    "NotionErrorClassifier",
    "NotionFileRef",
    "NotionNotSharedError",
    "NotionObjectNotFoundError",
    "NotionPropertyNotFoundError",
    "NotionRateLimitedError",
    "NotionTokenCredential",
    "NotionTokenMissingError",
    "NotionUnsupportedMarkdownError",
    "NotionUnwritablePropertyError",
    "NotionWriteNotLandedError",
    "PagePropertyWriter",
    "PropertyWrite",
    "PropertyWriteResult",
    "ResolvedSection",
    "SectionLocator",
    "SectionWriteResult",
    "SectionWriter",
    "build_blocks",
    "build_property_write",
    "comment_text",
    "download_notion_file",
    "heading_block",
    "literal_rich_text",
    "normalize_heading",
    "normalize_object_id",
    "option_name",
    "page_property",
    "plain_property_value",
    "property_type",
    "resolve_signed_url",
    "rich_text",
    "rich_text_to_markdown",
]
