"""Pre-commit hook: regenerate management-commands reference when command source changes.

Walks the Django management command tree in-process (no subprocess spawning)
and writes ``docs/generated/management-commands.md``.  Auto-stages the file on
change.
"""

import os
import subprocess
import sys
from pathlib import Path

_DEFAULT_OUTPUT = Path("docs/generated/management-commands.md")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    output = Path(args[0]) if args else _DEFAULT_OUTPUT

    old = output.read_text(encoding="utf-8") if output.is_file() else ""

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    import django

    django.setup()

    from teatree.core.management_commands_doc import write_management_commands_doc

    output.parent.mkdir(parents=True, exist_ok=True)
    write_management_commands_doc(output.parent)

    markdown = output.read_text(encoding="utf-8")

    if markdown != old and output == _DEFAULT_OUTPUT:
        subprocess.run(["git", "add", str(output)], check=False)
        json_path = output.with_suffix(".json")
        subprocess.run(["git", "add", str(json_path)], check=False)
        print(f"Updated {output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
