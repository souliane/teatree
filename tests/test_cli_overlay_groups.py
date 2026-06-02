"""The overlay CLI's hardcoded DJANGO_GROUPS must expose every ticket subcommand.

`t3 <overlay> ticket <sub>` dispatch is driven by the explicit
``DJANGO_GROUPS`` table in ``teatree.cli.overlay``. A subcommand absent from
that table is unreachable via the overlay CLI even though the core
``ticket`` management command defines it — exactly the regression this guards.
"""

from teatree.cli.overlay import DJANGO_GROUPS
from teatree.core.management.commands.lifecycle import Command as LifecycleCommand


def _ticket_subcommands() -> set[str]:
    return {name for name, _desc in DJANGO_GROUPS["ticket"].subcommands}


def _lifecycle_subcommands() -> set[str]:
    return {name for name, _desc in DJANGO_GROUPS["lifecycle"].subcommands}


def test_ticket_group_exposes_comment() -> None:
    assert "comment" in _ticket_subcommands()


def test_ticket_group_exposes_known_subcommands() -> None:
    assert {"transition", "list", "sync-completions", "comment"} <= _ticket_subcommands()


def test_lifecycle_group_exposes_record_review_skill_run() -> None:
    assert "record-review-skill-run" in _lifecycle_subcommands()


def test_lifecycle_group_exposes_record_review_context() -> None:
    assert "record-review-context" in _lifecycle_subcommands()


def test_lifecycle_subcommands_map_to_real_command_methods() -> None:
    for name in _lifecycle_subcommands():
        assert hasattr(LifecycleCommand, name.replace("-", "_")), name
