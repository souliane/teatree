"""Pre-commit hook: regenerate the representative CLI-output fixture from the CLI.

Renders ``teatree.cli.cli_output_snapshot`` (the rendered ``--help`` of the canonical
``t3`` commands) and writes the canonical ``docs/generated/cli/representative-output.md``.
Auto-stages the result unless ``CLI_OUTPUT_SNAPSHOT_NO_STAGE`` is set — CI sets it for
the docs-drift step so the ``git diff --exit-code docs/generated`` gate (the same
entrypoint the FSM diagrams and dashboard snapshot use) catches a stale committed
fixture. Modelled on ``generate_cli_reference.py``; the render is a pure command-tree
function, so unlike the dashboard snapshot it needs no test database.

See: souliane/teatree#12
"""

import os
import subprocess
import sys
from pathlib import Path

_CANONICAL = Path("docs/generated/cli/representative-output.md")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main() -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    src = repo_root() / "src"
    if src.is_dir():
        sys.path.insert(0, str(src))
    import django

    django.setup()

    from teatree.cli.cli_output_snapshot import render_cli_output_snapshot

    markdown = render_cli_output_snapshot()
    path = repo_root() / _CANONICAL
    if path.is_file() and path.read_text(encoding="utf-8") == markdown:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    if not os.environ.get("CLI_OUTPUT_SNAPSHOT_NO_STAGE"):
        subprocess.run(["git", "add", str(path)], check=False)
        print(f"Updated {_CANONICAL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
