"""Pure media-kind and magic-byte helpers for E2E evidence (#2156)."""

from teatree.utils.media import MediaKind, content_matches_kind, media_kind

_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff\xe0"
_GIF = b"GIF89a__"
_WEBP = b"RIFF\x00\x00\x00\x00WEBP"
_MP4 = b"\x00\x00\x00\x18ftypmp42"
_WEBM = b"\x1a\x45\xdf\xa3webm"
_OGG = b"OggS\x00\x00"
_HTML = b"<!DOCTYPE html>"


class TestMediaKind:
    def test_image_extensions(self) -> None:
        for name in ("a.png", "B.JPG", "c.jpeg", "d.gif", "e.webp"):
            assert media_kind(name) is MediaKind.IMAGE

    def test_video_extensions(self) -> None:
        for name in ("a.mp4", "B.WEBM", "c.mov", "d.m4v", "e.ogv"):
            assert media_kind(name) is MediaKind.VIDEO

    def test_other_extension(self) -> None:
        assert media_kind("notes.txt") is MediaKind.OTHER
        assert media_kind("data.json") is MediaKind.OTHER


class TestContentMatchesKind:
    def test_image_signatures_pass(self) -> None:
        for sig in (_PNG, _JPEG, _GIF, _WEBP):
            assert content_matches_kind(sig, MediaKind.IMAGE) is True

    def test_video_signatures_pass(self) -> None:
        for sig in (_MP4, _WEBM, _OGG):
            assert content_matches_kind(sig, MediaKind.VIDEO) is True

    def test_html_error_page_fails_both_kinds(self) -> None:
        # The exact failure the gate must catch: a 200 that served an HTML
        # sign-in / 404 page instead of the media bytes.
        assert content_matches_kind(_HTML, MediaKind.IMAGE) is False
        assert content_matches_kind(_HTML, MediaKind.VIDEO) is False

    def test_image_bytes_do_not_satisfy_video_kind(self) -> None:
        assert content_matches_kind(_PNG, MediaKind.VIDEO) is False

    def test_video_bytes_do_not_satisfy_image_kind(self) -> None:
        assert content_matches_kind(_MP4, MediaKind.IMAGE) is False

    def test_other_kind_never_passes(self) -> None:
        assert content_matches_kind(_PNG, MediaKind.OTHER) is False
