"""The overlay CLI's hardcoded DJANGO_GROUPS must expose every ticket subcommand.

`t3 <overlay> ticket <sub>` dispatch is driven by the explicit
``DJANGO_GROUPS`` table in ``teatree.cli.overlay``. A subcommand absent from
that table is unreachable via the overlay CLI even though the core
``ticket`` management command defines it — exactly the regression this guards.
"""

from teatree.cli.overlay import DJANGO_GROUPS


def _ticket_subcommands() -> set[str]:
    _help, subcommands = DJANGO_GROUPS["ticket"]
    return {name for name, _desc in subcommands}


def test_ticket_group_exposes_comment() -> None:
    assert "comment" in _ticket_subcommands()


def test_ticket_group_exposes_known_subcommands() -> None:
    assert {"transition", "list", "sync-completions", "comment"} <= _ticket_subcommands()
