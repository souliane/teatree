"""The Notion write roots configured for an overlay, read on the side of the boundary that may read settings."""

from teatree.config import get_effective_settings


def notion_write_roots(overlay: str | None) -> tuple[list[str], list[str]]:
    settings = get_effective_settings(overlay)
    return settings.notion_write_allowed_roots, settings.notion_write_denied_roots
