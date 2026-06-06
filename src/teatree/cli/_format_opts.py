"""Shared ``--format`` option vocabulary + validation for CLI commands.

Several ``t3`` commands accept ``--format text|json``; the valid set and the
"unknown --format" rejection had been copy-pasted. This is the one place that
owns the vocabulary and the exit-2 rejection.
"""

import typer

VALID_FORMATS = ("text", "json")


def require_valid_format(output_format: str) -> None:
    """Exit 2 with a clear message when ``output_format`` is not text/json."""
    if output_format not in VALID_FORMATS:
        typer.echo(f"unknown --format {output_format!r}; use 'text' or 'json'", err=True)
        raise typer.Exit(code=2)


__all__ = ["VALID_FORMATS", "require_valid_format"]
