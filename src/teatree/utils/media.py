"""Pure media-kind detection and renderability helpers for E2E evidence (#2156).

Two concerns, both deterministic and network-free so they unit-test in
isolation.

:func:`media_kind` classifies a file path's extension into the kind of embed
GitLab renders for it — an ``<img>`` for screenshots, a ``<video>`` for clips
— so the evidence builder embeds the right markdown and the verification gate
knows which magic bytes to expect.

:func:`content_matches_kind` magic-byte-checks a fetched upload's bytes against
its expected kind. GitLab serves every upload as ``application/octet-stream``
(the content-type header proves nothing) and a broken/expired upload serves an
HTML error page, so the only reliable proof that an embed will render is that
the fetched bytes carry the medium's own signature.

The recorded clips are VP8/WebM, which a Chromium browser plays natively, so
the evidence path uploads them as-is — the broken video player the user saw
was the same unresolvable relative-URL bug as the broken stills, not a codec
problem. No re-encoding is performed.
"""

from enum import StrEnum
from pathlib import Path

# Number of leading bytes a caller should fetch to satisfy every signature
# check below (the MP4 ``ftyp`` box lives at offset 4..12).
MAGIC_PREFIX_LEN = 16

_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_VIDEO_EXTS = frozenset({".mp4", ".webm", ".mov", ".m4v", ".ogv"})


class MediaKind(StrEnum):
    """How GitLab renders a markdown-embedded upload of this file."""

    IMAGE = "image"
    VIDEO = "video"
    OTHER = "other"


def media_kind(path: str | Path) -> MediaKind:
    """Classify *path* by extension into the kind of embed GitLab renders."""
    suffix = Path(path).suffix.lower()
    if suffix in _IMAGE_EXTS:
        return MediaKind.IMAGE
    if suffix in _VIDEO_EXTS:
        return MediaKind.VIDEO
    return MediaKind.OTHER


def content_matches_kind(data: bytes, kind: MediaKind) -> bool:
    """True when *data*'s magic bytes match the expected media *kind*.

    Catches the two failure modes a token HEAD/GET cannot distinguish by
    status alone: a non-image/non-video error body (HTML sign-in / 404 page)
    served on the same route, and a corrupt upload. ``OTHER`` cannot be
    magic-checked, so it never passes — the evidence path only embeds images
    and video.
    """
    if kind is MediaKind.IMAGE:
        return _is_image(data)
    if kind is MediaKind.VIDEO:
        return _is_video(data)
    return False


def _is_image(data: bytes) -> bool:
    return (
        data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"))  # PNG / JPEG
        or data[:6] in {b"GIF87a", b"GIF89a"}  # GIF
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")  # WebP
    )


def _is_video(data: bytes) -> bool:
    # ISO base media (MP4/MOV/M4V): a 'ftyp' box at offset 4.
    if data[4:8] == b"ftyp":
        return True
    # Matroska / WebM: EBML header magic.
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return True
    # Ogg (ogv).
    return data.startswith(b"OggS")
