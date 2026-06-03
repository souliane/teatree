"""Pre-commit hook: BLUEPRINT corpus size budget (#1128).

The BLUEPRINT is functional + architectural, not a prose mirror of the
code. The budget is a backstop against implementation prose creeping
back into the corpus (the ``skill-prose-ban`` #140 precedent: a rule the
user keeps restating becomes a deterministic gate).

**Budget philosophy — human readability wins over byte-minimization.**
The cap is a backstop, not a tight ceiling to defend row-by-row.
Defending a tight cap forced sections into undigestible walls of text —
run-on paragraphs with no blank lines, table cells stuffed with
multi-sentence prose. That is the wrong trade: the BLUEPRINT must read
well for humans. So the budget sits **generously above the live corpus**
with ample headroom, and raising it for a legitimate reviewed addition
(or a readability restoration that adds whitespace) is the expected
maintenance action, not a last-resort escape hatch.

Budget (bytes):

- Top-level ``BLUEPRINT.md``:     90 000  (~88 KB)
- ``docs/blueprint/`` corpus:    122 000  (~119 KB)
- Combined corpus total:         212 000  (~207 KB)

BLUEPRINT.md is a SINGLE file by user decision — never split, never
consolidate-by-splitting. The top-level budget sits comfortably above
the live file so the doc can keep growing as one document without the
override being load-bearing for ordinary edits.

Escape hatch: ``BLUEPRINT_SIZE_OVERRIDE=1`` skips the check. Prefer
raising the budget in this file over the override — the override is for
the rare in-flight commit, the raise is the durable fix.
"""

import os
import pathlib
import subprocess
import sys

_TOP_FILE = "BLUEPRINT.md"
_APPENDIX_DIR = "docs/blueprint"

# BLUEPRINT.md stays a single file (user veto). The live file (~83 KB)
# sits comfortably under this cap with the >=4 KB headroom the
# `TestRealCorpusFitsWithHeadroom` guard requires.
_BUDGET_TOP_LEVEL_BYTES = 90_000
# Sized generously above the live appendix corpus (~115 KB after the
# readability restoration) so per-row config/invariant additions and
# whitespace that aids reading both fit without a bump every PR. Raise
# this (and the total below) when a legitimate reviewed addition needs
# the room — that is the expected maintenance action, not a workaround.
_BUDGET_APPENDICES_BYTES = 122_000
# Live total corpus is ~198 KB; this leaves generous headroom above the
# >=4 KB guard. The coupling invariant the test enforces holds exactly:
# total - top_level (212,000 - 90,000 = 122,000) <= appendices (122,000),
# so the total budget can always admit a full top-level file plus a full
# appendix corpus.
_BUDGET_TOTAL_BYTES = 212_000


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def _size(path: pathlib.Path) -> int:
    return path.stat().st_size if path.is_file() else 0


def _appendix_total(root: pathlib.Path) -> int:
    appendix_dir = root / _APPENDIX_DIR
    if not appendix_dir.is_dir():
        return 0
    return sum(_size(p) for p in appendix_dir.glob("*.md"))


def _blueprint_touched() -> bool:
    """True when BLUEPRINT.md or any docs/blueprint/*.md is staged."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        check=False,
    )
    staged = result.stdout.splitlines()
    return any(f == _TOP_FILE or f.startswith(f"{_APPENDIX_DIR}/") for f in staged)


def main() -> int:
    if os.environ.get("BLUEPRINT_SIZE_OVERRIDE") == "1":
        return 0

    if not _blueprint_touched():
        return 0

    root = _repo_root()
    top_size = _size(root / _TOP_FILE)
    appendix_size = _appendix_total(root)
    total = top_size + appendix_size

    breaches: list[str] = []
    if top_size > _BUDGET_TOP_LEVEL_BYTES:
        breaches.append(f"{_TOP_FILE}: {top_size:,} B > budget {_BUDGET_TOP_LEVEL_BYTES:,} B")
    if appendix_size > _BUDGET_APPENDICES_BYTES:
        breaches.append(f"{_APPENDIX_DIR}/: {appendix_size:,} B > budget {_BUDGET_APPENDICES_BYTES:,} B")
    if total > _BUDGET_TOTAL_BYTES:
        breaches.append(f"corpus total: {total:,} B > budget {_BUDGET_TOTAL_BYTES:,} B")

    if not breaches:
        return 0

    print(file=sys.stderr)
    print("  BLUEPRINT corpus size budget FAILED (#1128):", file=sys.stderr)
    print(file=sys.stderr)
    for line in breaches:
        print(f"    - {line}", file=sys.stderr)
    print(file=sys.stderr)
    print(
        "  The BLUEPRINT is architectural, not a prose mirror of the code.",
        file=sys.stderr,
    )
    print(
        "  Move implementation detail to docstrings, --help text, CLAUDE.md,",
        file=sys.stderr,
    )
    print(
        "  AGENTS.md, or the issue tracker. To bypass for a reviewed bump:",
        file=sys.stderr,
    )
    print("    BLUEPRINT_SIZE_OVERRIDE=1 git commit ...", file=sys.stderr)
    print(file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
