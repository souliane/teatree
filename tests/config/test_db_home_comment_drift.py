# test-path: cross-cutting
"""``ban_close_trailers_on_namespaces`` docs do not lie about its home (#2697).

The setting is DB-home (#1775), not parsed from ``[teatree.publish_gates]``. Two
source files documented it as TOML-parsed; this drift guard goes RED if either
comment regresses to describing a TOML home. Anti-vacuous: it scans the exact
window around the setting name and fails on the legacy TOML-home phrasing.
"""

import inspect
from pathlib import Path

import teatree.config.settings as settings_mod
import teatree.core.close_trailer_scanner as close_trailer_mod

_BANNED_PHRASES = ("[teatree.publish_gates]", "parsed from", "Parsed from")
_SETTING = "ban_close_trailers_on_namespaces"


def _window_around(text: str, anchor: str, *, radius: int = 400) -> str:
    idx = text.find(anchor)
    assert idx != -1, f"anchor {anchor!r} not found"
    return text[max(0, idx - radius) : idx + radius]


def test_settings_field_comment_does_not_claim_toml_home() -> None:
    source = Path(inspect.getfile(settings_mod)).read_text(encoding="utf-8")
    window = _window_around(source, f"{_SETTING}: list[str]")
    for phrase in _BANNED_PHRASES:
        assert phrase not in window, f"stale TOML-home phrasing {phrase!r} near {_SETTING}"
    assert "DB-home" in window


def test_close_trailer_scanner_docstring_does_not_claim_toml_home() -> None:
    doc = close_trailer_mod.__doc__ or ""
    assert "[teatree.publish_gates]" not in doc
    assert "DB-home" in doc
