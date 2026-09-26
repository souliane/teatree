"""Drift locks on the `ticket transition` allow-list: it mirrors the FSM, and the help mirrors it."""

from teatree.core.management.commands._transition_names import ALLOWED_TRANSITIONS
from teatree.core.management.commands.ticket import Command
from teatree.core.models.ticket import Ticket

# ``unignore`` restores a *stored* previous state, which django-fsm cannot express as a
# static ``target``, so it guards itself instead of carrying ``@transition``.
NON_FSM_STATE_MUTATORS = frozenset({"unignore"})


def _fsm_transition_names() -> set[str]:
    """Every ``Ticket.state`` transition django-fsm knows about, derived from the field itself."""
    field = Ticket._meta.get_field("state")
    return {transition.name for transition in field.get_all_transitions(Ticket)}


def _transition_command_help() -> str:
    """The effective help typer surfaces for `ticket transition` (``help=`` else docstring)."""
    stack = [Command().typer_app]
    while stack:
        current = stack.pop()
        for info in current.registered_commands:
            name = info.name or (info.callback.__name__ if info.callback else "")
            if name == "transition":
                doc = info.callback.__doc__ if info.callback else None
                return info.help or doc or ""
        stack.extend(group.typer_instance for group in current.registered_groups)
    return ""


def _documented_transition_names(help_text: str) -> set[str]:
    """The comma-separated names listed after 'transition names:' in the help."""
    _, _, listed = help_text.partition("transition names:")
    return {name.strip() for name in listed.split(".", 1)[0].split(",") if name.strip()}


def test_help_documents_exactly_allowed_transitions() -> None:
    help_text = _transition_command_help()
    assert "transition names:" in help_text  # command found and carries the derived help
    assert _documented_transition_names(help_text) == set(ALLOWED_TRANSITIONS)


def test_every_fsm_transition_is_cli_dispatchable() -> None:
    unreachable = _fsm_transition_names() - ALLOWED_TRANSITIONS
    assert not unreachable, f"FSM offers these but the CLI refuses them as unknown: {sorted(unreachable)}"


def test_allow_list_adds_only_declared_non_fsm_mutators() -> None:
    assert ALLOWED_TRANSITIONS - _fsm_transition_names() == NON_FSM_STATE_MUTATORS


def test_declared_non_fsm_mutators_are_real_self_guarding_methods() -> None:
    assert NON_FSM_STATE_MUTATORS.isdisjoint(_fsm_transition_names())
    assert all(callable(getattr(Ticket, name, None)) for name in NON_FSM_STATE_MUTATORS)
