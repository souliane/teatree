r"""Deferred-redesign behaviors for the gate collapse (#126) — RED evals, xfail.

The full gate redesign (DESIGN SPEC, IMPL PLAN TDD-1, TDD-4..TDD-9) is too
large for the one-step-collapse PR that makes the live-post friction go
away. These tests pin the DESIRED post-redesign behavior of the deferred
increments so they are NOT silently dropped: each is marked
``xfail(strict=True)`` with the tracking item it belongs to, and flips to a
hard failure the moment the behavior lands — at which point the marker is
removed and the test becomes a live guard.

The assertions deliberately exercise only the *observable CLI surface*
(unknown options / unknown subcommands exit non-zero today), so the file
carries no static import of the not-yet-existent ``PostAuthorization``
model — the type checker stays green while the eval stays genuinely RED.

Deferred increments (each its own follow-up PR):

* TDD-1  — ``PostAuthorization`` model with ``uses_remaining`` / standing /
            ``expires_at`` replacing the two parallel approval tables.
* TDD-4  — ``authorize --uses N`` / ``--standing`` multi-use semantics
            (one authorize, N live posts) — needs the TDD-1 model.
* TDD-9  — Slack-phrase control of the master switch.

TDD-6 (``danger_gate_fail_open`` master switch + self-rescue allow-list) has
LANDED — its eval below is now a live guard, no longer xfail.
"""

import pytest
from typer.testing import CliRunner

from teatree.cli import app

pytestmark = pytest.mark.django_db

_runner = CliRunner()


class TestDeferredRedesignBehaviors:
    """RED evals for the deferred redesign increments — xfail until they land."""

    @pytest.mark.xfail(reason="TDD-1/TDD-4 deferred: authorize --uses N multi-use semantics", strict=True)
    def test_authorize_uses_n_flag_accepted(self) -> None:
        # One `authorize --uses 5` should be accepted (and later let five
        # sequential live posts through). Today the flag is unknown → exit 2.
        result = _runner.invoke(
            app,
            ["review", "authorize", "org/repo!7", "--approver", "U-OPERATOR", "--uses", "5"],
        )
        assert result.exit_code == 0, result.output

    @pytest.mark.xfail(reason="TDD-1/TDD-4 deferred: authorize --standing rows", strict=True)
    def test_authorize_standing_flag_accepted(self) -> None:
        result = _runner.invoke(
            app,
            ["review", "authorize", "org/repo!7", "--approver", "U-OPERATOR", "--standing"],
        )
        assert result.exit_code == 0, result.output

    def test_danger_gate_fail_open_status_command_exists(self) -> None:
        # TDD-6 landed (NEVER-LOCKOUT): the master fail-open switch is now a
        # live ``t3 review gate fail-open status`` command — this flipped from
        # an xfail deferral marker to a live guard.
        result = _runner.invoke(app, ["review", "gate", "fail-open", "status"])
        assert result.exit_code == 0, result.output
