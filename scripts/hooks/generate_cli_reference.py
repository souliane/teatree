"""Pre-commit hook: regenerate CLI reference when CLI source files change.

Walks the Typer app in-process (no subprocess spawning) and writes
``docs/generated/cli-reference.md``. The render is byte-deterministic across
environments (pinned width, no inherited tty/COLUMNS, home dir folded to ``~``).
Auto-stages the file on change unless ``CLI_REFERENCE_NO_STAGE`` is set — CI sets
it for the docs-drift step so ``git diff`` (no ``--cached``) actually catches a
stale committed reference instead of seeing an empty working-tree-vs-index diff.

See: souliane/teatree#67, souliane/teatree#2599
"""

import os
import subprocess
import sys
from pathlib import Path

_DEFAULT_OUTPUT = Path("docs/generated/cli-reference.md")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    output = Path(args[0]) if args else _DEFAULT_OUTPUT

    old = output.read_text(encoding="utf-8") if output.is_file() else ""

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    import django

    django.setup()

    from teatree.cli import app, register_overlay_commands
    from teatree.cli_reference import render_cli_reference_deterministic

    register_overlay_commands(allowlist={"t3-teatree"})
    markdown = render_cli_reference_deterministic(app)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")

    if markdown != old and output == _DEFAULT_OUTPUT and not os.environ.get("CLI_REFERENCE_NO_STAGE"):
        subprocess.run(["git", "add", str(output)], check=False)
        print(f"Updated {output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
