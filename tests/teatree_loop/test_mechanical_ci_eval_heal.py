"""CI-eval heal mechanical handler wiring (#3201 PR-3a).

The ``ci_eval_heal.advance`` signal must route to the ``advance_ci_eval_heal``
mechanical executor (the scanner→dispatch→handler seam), and the handler must be
best-effort — a failing advance pass logs and is swallowed, never raised into the tick.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.loop.dispatch_tables import MECHANICAL_BY_KIND
from teatree.loop.mechanical import HANDLERS, advance_ci_eval_heal


class TestWiring(TestCase):
    def test_kind_routes_to_the_mechanical_handler(self) -> None:
        assert MECHANICAL_BY_KIND["ci_eval_heal.advance"] == ("mechanical", "advance_ci_eval_heal")

    def test_handler_is_registered(self) -> None:
        assert HANDLERS["advance_ci_eval_heal"] is advance_ci_eval_heal


class TestBestEffort(TestCase):
    def test_advance_pass_failure_is_swallowed(self) -> None:
        with patch(
            "teatree.loop.mechanical_ci_eval_heal.advance_open_sessions",
            side_effect=RuntimeError("gh stalled"),
        ):
            advance_ci_eval_heal({})  # must not raise

    def test_delegates_to_advance_open_sessions(self) -> None:
        with patch("teatree.loop.mechanical_ci_eval_heal.advance_open_sessions") as advance:
            advance.return_value = type("R", (), {"outcomes": [], "errors": {}})()
            advance_ci_eval_heal({"open_count": 3})
        advance.assert_called_once_with()
