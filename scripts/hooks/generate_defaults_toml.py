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
from teatree.config.declared_defaults import declared_defaults, render_defaults_file

STATUSLINE_SH = Path(__file__).resolve().parents[2] / "hooks" / "scripts" / "statusline.sh"
_AUTOLOAD_BEGIN = "# BEGIN GENERATED: autoload default"
_AUTOLOAD_END = "# END GENERATED: autoload default"


def render_statusline_default(text: str) -> str:
    default = declared_defaults()["autoload"]
    if type(default) is not bool:
        message = "autoload declaration must be boolean"
        raise TypeError(message)
    if text.count(_AUTOLOAD_BEGIN) != 1 or text.count(_AUTOLOAD_END) != 1:
        message = "statusline autoload default markers must each occur once"
        raise ValueError(message)
    start = text.index(_AUTOLOAD_BEGIN)
    end = text.index(_AUTOLOAD_END) + len(_AUTOLOAD_END)
    if start >= end:
        message = "statusline autoload default markers are out of order"
        raise ValueError(message)
    block = f"{_AUTOLOAD_BEGIN}\n_AUTOLOAD_DEFAULT={'on' if default else 'off'}\n{_AUTOLOAD_END}"
    return text[:start] + block + text[end:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite and stage the file instead of reporting drift")
    args = parser.parse_args(argv)

    defaults_text = DEFAULTS_TOML.read_text(encoding="utf-8")
    statusline_text = STATUSLINE_SH.read_text(encoding="utf-8")
    changes = [
        (DEFAULTS_TOML, defaults_text, render_defaults_file(defaults_text)),
        (STATUSLINE_SH, statusline_text, render_statusline_default(statusline_text)),
    ]
    drifted = [(path, before, after) for path, before, after in changes if before != after]
    if not drifted:
        return 0
    if args.write:
        for path, _before, after in drifted:
            path.write_text(after, encoding="utf-8")
            generated_doc_staging.stage(path)
            print(f"rewrote {path}")
        return 0
    print("generated defaults have drifted from the declarations that state their values.")
    print("Regenerate and stage it:")
    print("  uv run python scripts/hooks/generate_defaults_toml.py --write")
    for path, before, after in drifted:
        sys.stdout.writelines(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                str(path),
                f"declared {path}",
            )
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
