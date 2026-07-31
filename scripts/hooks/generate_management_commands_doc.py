"""Pre-commit hook: regenerate management-commands reference when command source changes.

Walks the Django management command tree in-process (no subprocess spawning)
and writes ``docs/generated/management-commands.md``.  Auto-stages the file on
change.

``argv[0]`` selects the output path and is ALWAYS the file written, mirroring
``generate_cli_reference.py``. That is the merge-driver contract: when
``git_merge_generated`` runs this hook it passes git's ``%A`` output slot — a
``.merge_file_XXXXXX`` temp path in the repo root — and takes whatever is left
there as the merge result. Deriving a destination from ``argv[0].parent``
instead would leave the slot untouched (resolving the merge to "ours" while
reporting success) and drop the real output in the repo root. The ``.json``
sibling is written only alongside the committed default output, where the
docs-drift gate reads it.
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

    from teatree.core.management_commands_doc import (
        build_management_commands_doc_payload,
        render_management_commands_markdown,
        write_management_commands_doc,
    )

    output.parent.mkdir(parents=True, exist_ok=True)

    if output == _DEFAULT_OUTPUT:
        # The committed pair: the .md the docs-drift gate diffs plus its .json sibling.
        write_management_commands_doc(output.parent)
        markdown = output.read_text(encoding="utf-8")
    else:
        # An explicit output path (the merge driver's %A slot): write exactly that
        # file and nothing else, so the slot carries the regenerated result and no
        # stray sibling lands in whatever directory the slot happens to live in.
        markdown = render_management_commands_markdown(build_management_commands_doc_payload())
        output.write_text(markdown, encoding="utf-8")

    if markdown != old and output == _DEFAULT_OUTPUT:
        subprocess.run(["git", "add", str(output)], check=False)
        json_path = output.with_suffix(".json")
        subprocess.run(["git", "add", str(json_path)], check=False)
        print(f"Updated {output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
