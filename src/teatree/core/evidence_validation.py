"""Deterministic pre-post validation of E2E evidence images.

The preflight the ``e2e post-evidence`` command runs over every image the
manifest references BEFORE any upload or post. It refuses the whole post
(fail-loud) when an image is not a real piece of evidence, so the user never has
to manually spot a red-box-less screenshot or a pasted-twice look-alike after
the fact:

*   **Red-box gate** — every image must carry a red highlight box (the
    ``highlightAndShoot`` style is ``outline:3px solid red`` + a red box-shadow).
    A meaningful count of saturated-red pixels (``R≥200, G≤80, B≤80``) consistent
    with an outline must be present; a plain full-page screenshot with no
    highlight is refused by name. The threshold sits in the clean gap between
    real evidence crops (3660-6490 saturated-red px in the reference set) and the
    largest incidental red UI element in a plain shot (≈1667 px).
*   **Duplicate gate** — no two images may be byte-identical (the "several
    screenshots look the same" failure). The colliding pair is named.
*   **Staleness check** — an image dramatically older than the freshest in the
    set is WARNED about (not refused): a dev-environment image is legitimately
    frozen while a fresh local run posts alongside it.

Pure pixel/byte/mtime logic over on-disk paths — no ORM, no host, no network —
so it unit-tests in isolation. ``skip=True`` is the user-authorised escape hatch
(the agent never sets it on its own); it runs nothing dangerous, just returns no
warnings without refusing.
"""

import hashlib
from pathlib import Path

from PIL import Image, ImageChops

# A saturated-red pixel of the highlight outline: high red, low green/blue. The
# ``highlightAndShoot`` box is drawn ``outline:3px solid red`` + a red box-shadow.
_RED_MIN, _GREEN_MAX, _BLUE_MAX = 200, 80, 80

# Minimum saturated-red pixel count for an image to count as carrying a highlight
# box. Tuned against the reference evidence set: real ``highlightAndShoot`` crops
# carry ≥3660 saturated-red px, while the largest incidental red element in a
# plain (un-highlighted) screenshot is ≈1667 px — so 2000 sits cleanly between
# them, robust to both the UI-noise floor and the largest plain-page red element.
_RED_BOX_MIN_PIXELS = 2000

# An image whose mtime is older than the freshest image in its set by more than
# this many seconds is warned about (not refused): a frozen dev image is
# legitimate, but a much older one is worth flagging in case it is a leftover.
_STALENESS_WINDOW_SECONDS = 24 * 3600


class EvidenceImageValidationError(ValueError):
    """An evidence image failed a hard pre-post check — the post must NOT publish.

    Raised by :func:`validate_evidence_images` for a missing red box or a
    byte-identical duplicate pair, naming the exact offending file(s) and the
    reason. The command surfaces it as a non-zero ``SystemExit`` before any
    upload, so a failed validation burns no on-behalf approval and writes no note.
    """


def _saturated_red_pixel_count(path: Path) -> int:
    """Count the saturated-red pixels (the highlight-outline colour) in *path*.

    Builds a per-band ``"1"`` mask (``R≥_RED_MIN`` AND ``G≤_GREEN_MAX`` AND
    ``B≤_BLUE_MAX``) via vectorised band ``point`` thresholds + ``logical_and``,
    then counts the set pixels from the mask histogram — no per-pixel Python loop
    and no deprecated ``getdata`` call.
    """
    with Image.open(path) as img:
        r, g, b = img.convert("RGB").split()
    r_hi = r.point(lambda v: 255 if v >= _RED_MIN else 0).convert("1")
    g_lo = g.point(lambda v: 255 if v <= _GREEN_MAX else 0).convert("1")
    b_lo = b.point(lambda v: 255 if v <= _BLUE_MAX else 0).convert("1")
    mask = ImageChops.logical_and(ImageChops.logical_and(r_hi, g_lo), b_lo)
    return mask.histogram()[-1]


def has_red_highlight_box(path: Path) -> bool:
    """True when *path* carries a red highlight box (enough saturated-red pixels)."""
    return _saturated_red_pixel_count(path) >= _RED_BOX_MIN_PIXELS


def _refuse_images_without_red_box(images: list[Path]) -> None:
    """Raise naming every image that lacks a red highlight box."""
    missing = [img for img in images if not has_red_highlight_box(img)]
    if missing:
        names = ", ".join(img.name for img in missing)
        msg = (
            f"E2E evidence refused: no red highlight box found in {names}. "
            f"Every screenshot must carry the highlightAndShoot red box "
            f"(outline:3px solid red) — re-capture the missing one(s)."
        )
        raise EvidenceImageValidationError(msg)


def _refuse_duplicate_images(images: list[Path]) -> None:
    """Raise naming the first byte-identical pair of images."""
    seen: dict[str, Path] = {}
    for img in images:
        digest = hashlib.md5(img.read_bytes()).hexdigest()  # noqa: S324 — content key, not security.
        prior = seen.get(digest)
        if prior is not None:
            msg = (
                f"E2E evidence refused: {img.name} is byte-identical to {prior.name}. "
                f"Distinct screenshots must show distinct states — remove the duplicate "
                f"or re-capture the intended one."
            )
            raise EvidenceImageValidationError(msg)
        seen[digest] = img


def _staleness_warnings(images: list[Path]) -> list[str]:
    """Warn (do not refuse) for each image far older than the freshest in the set."""
    # A single image has no "freshest peer" to be stale against.
    if len(images) <= 1:
        return []
    mtimes = {img: img.stat().st_mtime for img in images}
    freshest = max(mtimes.values())
    warnings: list[str] = []
    for img, mtime in mtimes.items():
        age_gap = freshest - mtime
        if age_gap > _STALENESS_WINDOW_SECONDS:
            hours = age_gap / 3600
            warnings.append(
                f"E2E evidence warning: {img.name} is {hours:.0f}h older than the "
                f"freshest image in its set — confirm it is the intended (e.g. frozen "
                f"dev) capture and not a stale leftover."
            )
    return warnings


def validate_evidence_images(images: list[Path], *, skip: bool = False) -> list[str]:
    """Validate every evidence image; raise on a hard failure, return staleness warnings.

    Order: red-box gate (every image must carry a highlight box) → duplicate gate
    (no byte-identical pair) → staleness check (warn-only). A hard failure raises
    :class:`EvidenceImageValidationError` naming the offending file(s); the
    staleness check never raises — it returns a list of human-readable warning
    strings the caller surfaces loudly.

    ``skip=True`` is the user-authorised bypass (the agent never sets it itself):
    it short-circuits every check and returns no warnings, running nothing
    dangerous.
    """
    if skip:
        return []
    _refuse_images_without_red_box(images)
    _refuse_duplicate_images(images)
    return _staleness_warnings(images)
