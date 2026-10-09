"""CLI over :mod:`teatree.hooks.ai_signature_scan` — exit 0 = clean, 1 = a banned trailer was found."""

import sys
from pathlib import Path

import typer

from teatree.hooks.ai_signature_scan import scan_text, summary

app = typer.Typer(add_completion=False)


@app.command()
def main(
    input_file: str = typer.Argument("-", help="File to scan (- for stdin, or a file path)"),
    *,
    strict: bool = typer.Option(True, help="Exit 1 on any finding. --no-strict for warnings only."),
) -> None:
    """Scan a PR body / commit message for AI-signature / banned trailers."""
    text = sys.stdin.read() if input_file == "-" else Path(input_file).read_text(encoding="utf-8")
    findings = scan_text(text)
    print(summary(findings))
    if findings and strict:
        raise SystemExit(1)


if __name__ == "__main__":
    app()
