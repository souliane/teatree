"""Pre-commit hook: regenerate CLI reference when CLI source files change.

Walks the Typer app in-process (no subprocess spawning) and writes
``docs/generated/cli-reference.md``.  Auto-stages the file on change.

See: souliane/teatree#67
"""

import subprocess
import sys
from pathlib import Path

_DEFAULT_OUTPUT = Path("docs/generated/cli-reference.md")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    output = Path(args[0]) if args else _DEFAULT_OUTPUT

    old = output.read_text(encoding="utf-8") if output.is_file() else ""

    from teatree.cli import app
    from teatree.cli_reference import build_cli_reference_from_app

    markdown = build_cli_reference_from_app(app)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")

    if markdown != old and output == _DEFAULT_OUTPUT:
        subprocess.run(["git", "add", str(output)], check=False)
        print(f"Updated {output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
