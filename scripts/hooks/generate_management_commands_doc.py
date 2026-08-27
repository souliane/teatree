"""Pre-commit hook: regenerate management-commands reference when command source changes.

Walks the Django management command tree in-process (no subprocess spawning)
and writes ``docs/generated/management-commands.md``.  Auto-stages the file on
change.

``argv[0]`` selects the output path and is ALWAYS the file written, mirroring
``generate_cli_reference.py``. Callers that redirect the output rely on it —
``generate_all_docs --output-dir`` writes a drift check's scratch copy, never the
committed doc. Deriving a destination from ``argv[0].parent`` instead would leave
the caller's path untouched while reporting success, and drop the real output
beside it. The ``.json`` sibling is written only alongside the committed default
output, where the docs-drift gate reads it.
"""

import os
import sys
from pathlib import Path

import generated_doc_staging

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
        generated_doc_staging.stage(output)
        json_path = output.with_suffix(".json")
        generated_doc_staging.stage(json_path)
        print(f"Updated {output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
