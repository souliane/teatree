"""Deterministic render of the ``/dash/board`` page to byte-stable HTML (#3162).

The dashboard's cheap regression coverage — the ``core/factory/dashboard_snapshot``
pattern applied to the new board, NOT a pixel/e2e snapshot. Render the board over
an empty ticket set (so the DB state is fixed), freeze the per-request CSRF token,
and compare against the committed ``snapshots/board.html``. A template/nav/markup
regression reds the drift test; a screenshot suite is neither needed nor wanted for
a single-operator local tool.
"""

import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client
from django.test.utils import override_settings

_SNAPSHOT_USER = "dash-snapshot"
# The CSRF token is embedded once in the base template's ``hx-headers`` and is
# per-request volatile; freeze it so the committed snapshot is stable.
_CSRF_HX = re.compile(r'("X-CSRFToken": ")[^"]*(")')

SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "board.html"


def _canonical_html(html: str) -> str:
    frozen = _CSRF_HX.sub(r"\1CSRF\2", html)
    return "\n".join(line.rstrip() for line in frozen.split("\n"))


def render_board_snapshot() -> str:
    """Render ``/dash/board/`` to deterministic HTML (requires a usable DB).

    The caller owns the database — under pytest it is the per-test transaction.
    An empty ticket set keeps the render fixed regardless of machine state.
    """
    user_model = get_user_model()
    user, _ = user_model.objects.get_or_create(
        username=_SNAPSHOT_USER,
        defaults={"is_staff": True, "is_superuser": True, "is_active": True},
    )
    with override_settings(STATIC_URL="/static/", LANGUAGE_CODE="en-us", TIME_ZONE="UTC", DEBUG=False):
        client = Client()
        client.force_login(user)
        html = client.get("/dash/board/").content.decode("utf-8")
    return _canonical_html(html)
