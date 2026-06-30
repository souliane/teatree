"""The admin-dashboard snapshot renders deterministically and stays in sync.

The loud local gate for ``docs/generated/dashboard/admin-index.html`` — the analog of
``check_fsm_diagrams_sync.py`` for the FSM diagrams, run here in the test suite because
the admin index needs a database the docs-drift script provisions for itself. CI also
catches the same drift via ``git diff --exit-code docs/generated`` after regenerating.

See: souliane/teatree#12
"""

from pathlib import Path

from django.test import TestCase

from teatree.core.dashboard_snapshot import _canonical_html, render_dashboard_snapshot

_CANONICAL = Path(__file__).resolve().parents[2] / "docs/generated/dashboard/admin-index.html"


class DashboardSnapshotTests(TestCase):
    def test_committed_snapshot_is_in_sync(self) -> None:
        expected = _CANONICAL.read_text(encoding="utf-8")
        assert render_dashboard_snapshot() == expected, (
            "docs/generated/dashboard/admin-index.html is stale — regenerate it:\n"
            "  uv run python scripts/hooks/generate_dashboard_snapshot.py"
        )

    def test_render_is_byte_stable(self) -> None:
        assert render_dashboard_snapshot() == render_dashboard_snapshot()

    def test_snapshot_lists_the_core_domain_models(self) -> None:
        html = render_dashboard_snapshot()
        for href in ("/core/ticket/", "/core/worktree/", "/core/loop/", "/core/configsetting/"):
            assert href in html

    def test_volatile_csrf_token_is_frozen(self) -> None:
        assert 'name="csrfmiddlewaretoken" value="CSRF"' in render_dashboard_snapshot()
        two_distinct = (
            '<input name="csrfmiddlewaretoken" value="aaaaaaaa"><input name="csrfmiddlewaretoken" value="bbbbbbbb">'
        )
        assert _canonical_html(two_distinct) == (
            '<input name="csrfmiddlewaretoken" value="CSRF"><input name="csrfmiddlewaretoken" value="CSRF">'
        )

    def test_canonical_html_strips_trailing_whitespace(self) -> None:
        assert _canonical_html("<p>x</p>   \n<div>  </div>  ") == "<p>x</p>\n<div>  </div>"

    def test_committed_snapshot_has_no_trailing_whitespace(self) -> None:
        for line in _CANONICAL.read_text(encoding="utf-8").split("\n"):
            assert line == line.rstrip()
