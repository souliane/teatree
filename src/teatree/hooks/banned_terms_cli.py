"""File-scanning CLI for the banned-terms pre-commit hook.

``scripts/hooks/check-banned-terms.sh`` used to embed its OWN copy of the
whole-token tokenizer/matcher in bash-inlined Python. That copy could drift
from :mod:`teatree.hooks.term_match` (the matcher the in-process gates use)
without anything noticing — the #1839 migration claimed the shell hook
"mirrored" ``term_match`` but the duplicated bash implementation was a second
source of truth. This module removes that duplication: the shell hook now
shells out here, so EVERY banned-terms entry point (the shell hook, the
``banned_terms_scanner`` posting gate, and the ``check_no_overlay_leak``
core-leak gate) runs the SAME :mod:`teatree.hooks.term_match` code. A parity
meta-test pins them to identical verdicts on a golden corpus so they cannot
drift again.

Usage mirrors the old shell behaviour exactly::

    python -m teatree.hooks.banned_terms_cli --config <toml> <file> [<file> ...]

- exit 0: no file contains a banned term (or no config / no terms ⇒ no-op).
- exit 1: at least one file contains a banned term. The same
``BANNED TERM in <file>:`` report the shell hook printed is emitted, so the
``banned_terms_scanner`` report parser keeps working unchanged.

The TOML term list is read from the first section carrying a ``banned_terms``
array (matching the old shell extractor), and the email carve-out lives in
``term_match`` so it, too, is shared rather than duplicated.
"""

import argparse
import sys
import tomllib
from pathlib import Path

from teatree.hooks.term_match import file_matches


def _load_terms(config: Path) -> tuple[str, ...]:
    """Return the first ``banned_terms`` array found in the TOML config.

    Mirrors the old shell extractor: scan every top-level section (and the
    document root) for a ``banned_terms`` key and use the first one found.
    """
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ()
    for value in [*data.values(), data]:
        if isinstance(value, dict) and "banned_terms" in value:
            terms = value["banned_terms"]
            if isinstance(terms, list):
                return tuple(str(t).strip() for t in terms if str(t).strip())
    return ()


def main(argv: list[str]) -> int:  # pragma: no cover — CLI entry point (orchestrates tested helpers)
    parser = argparse.ArgumentParser(description="Reject files containing banned terms.")
    parser.add_argument("--config", required=True, help="TOML file with a banned_terms array.")
    parser.add_argument("files", nargs="*", help="Files to scan.")
    args = parser.parse_args(argv)

    config = Path(args.config).expanduser()
    if not config.is_file():
        return 0  # no config ⇒ no-op, matching the old shell behaviour
    terms = _load_terms(config)
    if not terms:
        return 0  # no terms ⇒ no-op

    report: list[str] = []
    for file in args.files:
        path = Path(file)
        if not path.is_file():
            continue
        hits = file_matches(str(path), terms)
        if not hits:
            continue
        report.append(f"BANNED TERM in {file}:")
        report.extend(f"  {line_number}:{line}" for line_number, _term, line in hits)

    if report:
        report.extend(("", f"Banned terms: {','.join(terms)}", "These terms must not appear in this repo."))
        sys.stdout.write("\n".join(report) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
