"""The `ticket transition` help must list exactly ALLOWED_TRANSITIONS (drift lock)."""

from teatree.core.management.commands._transition_names import ALLOWED_TRANSITIONS
from teatree.core.management.commands.ticket import Command


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
