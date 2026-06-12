"""Deterministic pre-post validation of E2E evidence images.

The preflight in :mod:`teatree.core.evidence_validation` refuses a post when ANY
image lacks a red highlight box or two images are byte-identical, and warns
(without refusing) when an image is dramatically older than the freshest in its
set. Pure pixel/byte/mtime logic — no ORM, no host, no network — so it
unit-tests in isolation against real PNGs written under ``tmp_path``.

The red-box threshold is tuned in the same way the command path tunes it: a
synthetic ``highlightAndShoot``-style box (a thick red outline) lands in the
saturated-red range the real evidence crops produce (3660-6490 px), while a
plain screenshot has none.
"""

import os
import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from teatree.core.evidence_validation import (
    EvidenceImageValidationError,
    has_red_highlight_box,
    validate_evidence_images,
)


def _red_boxed_png(path: Path, *, size: tuple[int, int] = (400, 300)) -> Path:
    """Write a PNG carrying a thick red outline box (passes the red-box gate)."""
    img = Image.new("RGB", size, (245, 245, 245))
    draw = ImageDraw.Draw(img)
    w, h = size
    for off in range(6):
        draw.rectangle([20 + off, 20 + off, w - 40 - off, h - 50 - off], outline=(220, 20, 20))
    img.save(path, "PNG")
    return path


def _plain_png(path: Path, *, fill: tuple[int, int, int] = (240, 240, 240)) -> Path:
    """Write a PNG with no red highlight box (fails the red-box gate)."""
    Image.new("RGB", (400, 300), fill).save(path, "PNG")
    return path


class TestHasRedHighlightBox:
    def test_red_boxed_png_detected(self, tmp_path: Path) -> None:
        assert has_red_highlight_box(_red_boxed_png(tmp_path / "boxed.png")) is True

    def test_plain_png_not_detected(self, tmp_path: Path) -> None:
        assert has_red_highlight_box(_plain_png(tmp_path / "plain.png")) is False

    def test_small_red_ui_accent_not_detected(self, tmp_path: Path) -> None:
        """A handful of red pixels (an error icon / asterisk) is not a highlight box."""
        img = Image.new("RGB", (400, 300), (240, 240, 240))
        draw = ImageDraw.Draw(img)
        draw.rectangle([5, 5, 12, 12], fill=(230, 10, 10))
        img.save(tmp_path / "accent.png", "PNG")
        assert has_red_highlight_box(tmp_path / "accent.png") is False


class TestRedBoxRefusal:
    def test_refuses_post_naming_the_no_red_box_file(self, tmp_path: Path) -> None:
        good = _red_boxed_png(tmp_path / "good.png")
        plain = _plain_png(tmp_path / "plain.png")
        with pytest.raises(EvidenceImageValidationError) as exc:
            validate_evidence_images([good, plain])
        assert "plain.png" in str(exc.value)

    def test_all_good_red_boxed_distinct_set_passes(self, tmp_path: Path) -> None:
        a = _red_boxed_png(tmp_path / "a.png")
        b = _red_boxed_png(tmp_path / "b.png", size=(420, 320))
        # Returns warnings (here none); does not raise.
        assert validate_evidence_images([a, b]) == []


class TestDuplicateRefusal:
    def test_refuses_byte_identical_pair_naming_both(self, tmp_path: Path) -> None:
        a = _red_boxed_png(tmp_path / "first.png")
        dup = tmp_path / "second.png"
        dup.write_bytes(a.read_bytes())
        with pytest.raises(EvidenceImageValidationError) as exc:
            validate_evidence_images([a, dup])
        message = str(exc.value)
        assert "first.png" in message
        assert "second.png" in message

    def test_distinct_images_are_not_flagged_as_duplicates(self, tmp_path: Path) -> None:
        a = _red_boxed_png(tmp_path / "a.png")
        b = _red_boxed_png(tmp_path / "b.png", size=(440, 300))
        assert validate_evidence_images([a, b]) == []


class TestStaleness:
    def test_stale_image_warns_but_does_not_refuse(self, tmp_path: Path) -> None:
        fresh = _red_boxed_png(tmp_path / "fresh.png")
        stale = _red_boxed_png(tmp_path / "stale.png", size=(420, 300))
        old = time.time() - (48 * 3600)
        os.utime(stale, (old, old))
        warnings = validate_evidence_images([fresh, stale])
        assert any("stale.png" in w for w in warnings)

    def test_co_temporal_images_do_not_warn(self, tmp_path: Path) -> None:
        a = _red_boxed_png(tmp_path / "a.png")
        b = _red_boxed_png(tmp_path / "b.png", size=(420, 300))
        now = time.time()
        os.utime(a, (now, now))
        os.utime(b, (now, now))
        assert validate_evidence_images([a, b]) == []


class TestSkipValidation:
    def test_skip_returns_no_warnings_and_does_not_refuse(self, tmp_path: Path) -> None:
        plain = _plain_png(tmp_path / "plain.png")
        dup = tmp_path / "dup.png"
        dup.write_bytes(plain.read_bytes())
        # Even a no-red-box duplicate pair passes when validation is skipped.
        assert validate_evidence_images([plain, dup], skip=True) == []
