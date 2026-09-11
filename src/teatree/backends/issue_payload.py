"""The payload-shape guard both issue classifiers share.

``CodeHostBackend.get_issue`` answers with the forge's raw issue object, an
``{"error": ...}`` envelope, or — from a backend that could not resolve the URL —
something that is not a mapping at all. No verdict can be read out of the last two,
and both classifiers collapse them to their own UNKNOWN, so the check lives once here.
"""

from typing import cast

from teatree.types import RawAPIDict


def payload_or_none(issue_data: object) -> RawAPIDict | None:
    """The issue payload itself, or ``None`` for a shape that carries no verdict at all."""
    if not isinstance(issue_data, dict):
        return None
    payload = cast("RawAPIDict", issue_data)
    return None if "error" in payload else payload
