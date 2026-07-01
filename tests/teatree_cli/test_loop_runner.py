"""``t3 loop-runner`` CLI callback — forwards ``--once`` to the mgmt command (#2876).

``loop_runner`` is the thin arg-forwarding wrapper the ``t3 loop-runner`` console
entry dispatches to: it bootstraps Django, then hands off to the ``loop_runner``
management command (which owns the singleton flock + daemon). These pin the
forwarding contract so the wrapper can't silently stop threading ``--once``
through — the callback under test performs a real ``call_command`` dispatch, only
Django bootstrap and the mgmt command itself are stubbed.
"""

from unittest.mock import patch

from teatree.cli.loop_runner import loop_runner


class TestLoopRunnerCallbackForwarding:
    def test_once_forwards_once_kwarg_to_management_command(self) -> None:
        with (
            patch("teatree.cli.loop_runner.ensure_django") as ensure,
            patch("django.core.management.call_command") as call,
        ):
            loop_runner(once=True)

        ensure.assert_called_once_with()
        call.assert_called_once_with("loop_runner", once=True)

    def test_default_forwards_no_kwargs_to_management_command(self) -> None:
        with (
            patch("teatree.cli.loop_runner.ensure_django") as ensure,
            patch("django.core.management.call_command") as call,
        ):
            loop_runner(once=False)

        ensure.assert_called_once_with()
        call.assert_called_once_with("loop_runner")
