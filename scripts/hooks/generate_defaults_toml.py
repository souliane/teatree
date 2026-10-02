"""Re-render ``defaults.toml``'s ``[teatree]`` table from the declarations that state it.

The declarations in ``teatree.config.declared_defaults`` are the authority; this writes
their export. Without ``--write`` it only reports drift, exiting 1.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from teatree.config.cold_defaults import DEFAULTS_TOML
from teatree.config.declared_defaults import render_defaults_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the file instead of reporting drift")
    args = parser.parse_args(argv)

    committed = DEFAULTS_TOML.read_text(encoding="utf-8")
    rendered = render_defaults_file(committed)
    if rendered == committed:
        return 0
    if args.write:
        DEFAULTS_TOML.write_text(rendered, encoding="utf-8")
        print(f"rewrote {DEFAULTS_TOML}")
        return 0
    print(f"{DEFAULTS_TOML} drifted from its declarations; run {Path(__file__).name} --write")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
