"""Re-render ``defaults.toml``'s ``[teatree]`` table from the declarations that state it.

The declarations in ``teatree.config.declared_defaults`` are the authority; this writes
their export. Without ``--write`` it reports drift with a unified diff, exiting 1.
``--write`` rewrites and stages the result for the commit-stage hook.
"""

import argparse
import difflib
import sys
from pathlib import Path

import generated_doc_staging

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from teatree.config.cold_defaults import DEFAULTS_TOML
from teatree.config.declared_defaults import render_defaults_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite and stage the file instead of reporting drift")
    args = parser.parse_args(argv)

    committed = DEFAULTS_TOML.read_text(encoding="utf-8")
    rendered = render_defaults_file(committed)
    if rendered == committed:
        return 0
    if args.write:
        DEFAULTS_TOML.write_text(rendered, encoding="utf-8")
        generated_doc_staging.stage(DEFAULTS_TOML)
        print(f"rewrote {DEFAULTS_TOML}")
        return 0
    print("defaults.toml has drifted from the declarations that state its values.")
    print("Regenerate and stage it:")
    print("  uv run python scripts/hooks/generate_defaults_toml.py --write")
    sys.stdout.writelines(
        difflib.unified_diff(
            committed.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            "committed",
            "declared",
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
