"""``t3 loop claim-next`` — the canonical atomic-claim CLI command (#1107 Prong C).

Split out of ``teatree.cli.loop`` so that file stays under the
module-health public-function cap (the same split rationale as
``cli/loop_slack_answer.py``). This is a thin wrapper that mirrors the
``pending-spawn``/``spawn-claim`` wrappers in ``cli/loop.py``: bootstrap
Django, then delegate to the ``loop_dispatch claim-next`` mgmt command
(the #786 WS1 atomic claim ``Task.objects.claim_next_pending``).

#1107 — although ``loop_dispatch`` DOES expose the ``claim-next``
subcommand and the BLUEPRINT, the Stop-hook self-pump, ``cli/loop.py``'s
help text, and the slack-answer cycle all standardise on
``t3 loop claim-next``, ``cli/loop.py`` only wired ``pending-spawn`` /
``spawn-claim``. So the canonical command errored "No such command",
silently breaking the claim-next spawn pump. This wrapper restores it.

The assembled callable is registered on the parent ``loop_app`` by
``cli/loop.py`` (``loop_app.command("claim-next")(claim_next_command)``)
— a one-line registration so the sibling module avoids a circular import
on ``loop_app`` itself.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django


def claim_next_command(
    *,
    claimed_by: str = typer.Option("", "--claimed-by", help="Worker identifier stored on the claim."),
    json_output: bool = typer.Option(False, "--json", help="Emit the claimed dispatch as JSON."),
) -> None:
    """Atomically claim the oldest pending dispatchable Task, then emit it."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415

    kwargs: dict[str, str | bool] = {}
    if claimed_by:
        kwargs["claimed_by"] = claimed_by
    if json_output:
        kwargs["json_output"] = True
    call_command("loop_dispatch", "claim-next", **kwargs)
