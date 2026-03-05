#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "typer>=0.12",
# ]
# ///
"""Generate an HTML dashboard from followup.json.

Usage: generate_dashboard.py [INPUT] [OUTPUT]
    INPUT defaults to $T3_DATA_DIR/followup.json
    OUTPUT defaults to $T3_DATA_DIR/followup.html
"""

import json
import os
from pathlib import Path

import typer
from lib.dashboard_renderer import render_dashboard

_DEFAULT_DATA_DIR = Path.home() / ".local/share/teatree"


def main(
    input_path: Path | None = typer.Argument(None, help="Path to followup.json"),
    output_path: Path | None = typer.Argument(None, help="Path for output HTML"),
) -> None:
    data_dir = Path(os.environ.get("T3_DATA_DIR") or str(_DEFAULT_DATA_DIR))
    if input_path is None:
        input_path = data_dir / "followup.json"
    if output_path is None:
        output_path = data_dir / "followup.html"

    if not input_path.is_file():
        print(f"Error: {input_path} not found")
        raise SystemExit(1)

    data = json.loads(input_path.read_text(encoding="utf-8"))
    html = render_dashboard(data)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"Dashboard written to {output_path}")


if __name__ == "__main__":
    typer.run(main)
