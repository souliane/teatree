"""Shared ``--format`` option vocabulary + validation for CLI commands.

Several ``t3`` commands accept ``--format text|json``; the valid set and the
"unknown --format" rejection had been copy-pasted. This is the one place that
owns the vocabulary and the exit-2 rejection.
"""

import typer

VALID_FORMATS = ("text", "json")


def require_valid_format(output_format: str, valid: tuple[str, ...] = VALID_FORMATS) -> None:
    """Exit 2 with a clear message when ``output_format`` is not in ``valid``.

    ``valid`` defaults to text/json; a command that also renders another format
    (e.g. ``eval run`` adds ``html``) passes its own extended set so the extra
    format is accepted there without widening it for every other command.
    """
    if output_format not in valid:
        allowed = " or ".join(repr(fmt) for fmt in valid)
        typer.echo(f"unknown --format {output_format!r}; use {allowed}", err=True)
        raise typer.Exit(code=2)


__all__ = ["VALID_FORMATS", "require_valid_format"]
