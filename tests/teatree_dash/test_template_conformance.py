"""Django's ``{# … #}`` comment is single-line ONLY — a multi-line one renders as page text.

The lexer matches an opening and a closing tag on the SAME line, so a ``{#`` whose
``#}`` sits on a later line is not a comment at all: every line of it is emitted
verbatim into the page. Three of these had shipped, and the ``base.html`` one sat in
the header block, so the explanatory prose appeared centred in the header of every
dashboard page. ``{% comment %} … {% endcomment %}`` is the multi-line construct.

Whole-tree rather than three pinned assertions: the fourth one someone writes next
month must turn this RED too.
"""

from pathlib import Path

from django.test import TestCase

from teatree.dash.dashboard_snapshot import render_board_snapshot

_TEMPLATES = Path(__file__).resolve().parents[2] / "src/teatree/dash/templates"


def _unterminated_comment_openings() -> list[str]:
    return [
        f"{path.relative_to(_TEMPLATES)}:{number}: {line.strip()}"
        for path in sorted(_TEMPLATES.rglob("*.html"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for index in range(len(line))
        if line.startswith("{#", index) and "#}" not in line[index:]
    ]


def test_no_dash_template_opens_a_comment_it_does_not_close_on_the_same_line() -> None:
    offenders = _unterminated_comment_openings()
    assert not offenders, (
        "these `{#` openings have no `#}` on the same line, so Django renders them as "
        "visible page text — use `{% comment %} … {% endcomment %}`:\n" + "\n".join(offenders)
    )


class RenderedBoardCarriesNoCommentMarkupTestCase(TestCase):
    def test_board_render_contains_no_comment_delimiters(self) -> None:
        html = render_board_snapshot()
        assert "{#" not in html, "a template comment leaked into the rendered page"
