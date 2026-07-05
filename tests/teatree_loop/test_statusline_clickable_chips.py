"""Every #N/!N chip is hyperlinked — canonical URL else tracker search (PR-17).

Item 1: an unknown/404'd canonical URL must fall back to a clickable tracker
search URL, never render as bare text.
"""

from unittest.mock import patch

from teatree.loop.rendering import _overlay_search_base
from teatree.loop.rendering_items import _effective_url, _LinkCtx, _PRRef, _render_canonical_item, _search_term
from teatree.loop.rendering_zones import _link


class TestEffectiveUrl:
    def test_canonical_http_url_is_kept(self) -> None:
        assert _effective_url("https://x/issues/42", "#42", "https://s/?q=") == "https://x/issues/42"

    def test_missing_url_falls_back_to_search(self) -> None:
        assert _effective_url("", "#214", "https://github.com/search?type=issues&q=") == (
            "https://github.com/search?type=issues&q=214"
        )

    def test_no_search_base_stays_bare(self) -> None:
        assert _effective_url("", "#42", "") == ""

    def test_search_term_prefers_number(self) -> None:
        assert _search_term("⚡#7") == "7"
        assert _search_term("!145") == "145"

    def test_search_term_slug_fallback(self) -> None:
        assert _search_term("fix the widget") == "fix the widget"


class TestOverlaySearchBase:
    def test_github_overlay_returns_github_search(self) -> None:
        overlay = _overlay_stub(code_host="")
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"acme": overlay}):
            assert _overlay_search_base("acme") == "https://github.com/search?type=issues&q="

    def test_gitlab_overlay_returns_gitlab_search(self) -> None:
        overlay = _overlay_stub(code_host="gitlab")
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"acme": overlay}):
            assert _overlay_search_base("acme") == "https://gitlab.com/search?scope=issues&search="

    def test_unregistered_overlay_is_bare(self) -> None:
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={}):
            assert _overlay_search_base("ghost") == ""

    def test_blank_overlay_is_bare(self) -> None:
        assert _overlay_search_base("") == ""


class TestChipHyperlinkedNotBare:
    def test_urlless_ticket_links_to_search_not_bare(self) -> None:
        ctx = _LinkCtx(colorize=False, link=_link, search_base="https://github.com/search?type=issues&q=")
        rendered = _render_canonical_item(label="#214", url="", title="deleted", child_refs=[], ctx=ctx)
        assert "https://github.com/search?type=issues&q=214" in rendered
        assert rendered != "#214"

    def test_urlless_chip_stays_bare_without_search_base(self) -> None:
        ctx = _LinkCtx(colorize=False, link=_link, search_base="")
        rendered = _render_canonical_item(label="#214", url="", title="", child_refs=[], ctx=ctx)
        assert rendered == "#214"


class TestChildMrChipsRenderOnePerRef:
    """Each child MR ref renders as one terse ``!N`` chip on the line (#1377) — no per-MR fan-out."""

    def test_every_child_mr_renders_one_chip(self) -> None:
        ctx = _LinkCtx(colorize=False, link=_link, search_base="")
        child_refs = [
            _PRRef(iid=1, url="https://x/mr/1"),
            _PRRef(iid=2, url="https://x/mr/2"),
            _PRRef(iid=3, url="https://x/mr/3"),
        ]
        rendered = _render_canonical_item(
            label="#7", url="https://x/issues/7", title="t", child_refs=child_refs, ctx=ctx
        )
        for iid in (1, 2, 3):
            assert f"!{iid}" in rendered, rendered


def _overlay_stub(*, code_host: str) -> object:
    class _Config:
        pass

    config = _Config()
    config.code_host = code_host

    class _Overlay:
        pass

    overlay = _Overlay()
    overlay.config = config
    return overlay
