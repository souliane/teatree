"""Pre-commit + CI gate: forbid overlay-scoped names in core.

BLUEPRINT § 1 ("Core stays generic"): no overlay-specific names appear
in `src/teatree/` or `docs/`. Per-overlay specifics live in the overlay
package and in `~/.teatree.toml`.

The list of forbidden tokens is loaded at runtime from
`$TEATREE_OVERLAY_LEAK_TERMS` (comma-separated) or from
`~/.teatree.toml` under `[overlay_leak].terms`. The public repo ships
with an empty default — each operator extends it locally with the
overlay-scoped names that must never reach core.

Whole-token matching (``teatree.hooks.term_match``) is used so a generic
word that merely *contains* a configured term as a substring does not
trigger — the SAME matcher the ``[teatree].banned_terms`` posting gate
uses. A term matches only when its own tokens appear as a contiguous run
of whole tokens, with ``-``, ``_``, whitespace, and punctuation all acting
as separators. See that module for the matching rules and the documented
trade-off.

Exit code: 0 if clean, 1 if any configured term appears in the scanned
files.
"""

import os
import subprocess
import sys
import tomllib
from pathlib import Path

from teatree.hooks.term_match import matched_term


def _load_terms() -> tuple[str, ...]:
    """Load forbidden tokens from env var or ~/.teatree.toml."""
    env = os.environ.get("TEATREE_OVERLAY_LEAK_TERMS", "")
    if env:
        return tuple(t.strip() for t in env.split(",") if t.strip())

    config_path = Path.home() / ".teatree.toml"
    if config_path.is_file():
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return ()
        terms = data.get("overlay_leak", {}).get("terms", [])
        if isinstance(terms, list):
            return tuple(str(t) for t in terms if isinstance(t, str) and t.strip())
    return ()


OVERLAY_LEAK_TERMS: tuple[str, ...] = _load_terms()

# Roots that must stay generic.
SCAN_ROOTS: tuple[str, ...] = ("src/teatree", "docs")

# Path globs scanned files must match (case-sensitive, suffix only).
TEXT_SUFFIXES: tuple[str, ...] = (
    ".py",
    ".md",
    ".rst",
    ".txt",
    ".html",
    ".yml",
    ".yaml",
    ".toml",
    ".json",
    ".sh",
    ".ts",
    ".js",
)


def _staged_files() -> list[Path]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [Path(line) for line in result.stdout.splitlines() if line.strip()]


def _walk_roots() -> list[Path]:
    paths: list[Path] = []
    for root in SCAN_ROOTS:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        paths.extend(p for p in root_path.rglob("*") if p.is_file())
    return paths


def _is_in_scan_roots(path: Path) -> bool:
    return any(str(path).startswith(f"{root}/") for root in SCAN_ROOTS)


def _scan(paths: list[Path]) -> list[tuple[Path, int, str, str]]:
    if not OVERLAY_LEAK_TERMS:
        return []
    findings: list[tuple[Path, int, str, str]] = []
    for path in paths:
        if path.suffix not in TEXT_SUFFIXES:
            continue
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            term = matched_term(line, OVERLAY_LEAK_TERMS)
            if term is not None:
                findings.append((path, lineno, term, line.strip()))
    return findings


def main(argv: list[str]) -> int:
    if argv:
        paths = [Path(p) for p in argv if _is_in_scan_roots(Path(p))]
    else:
        # Pre-commit invocation without args + CI invocation: walk the tree.
        staged = [p for p in _staged_files() if _is_in_scan_roots(p)]
        paths = staged or _walk_roots()

    findings = _scan(paths)
    if not findings:
        return 0

    print("Overlay-leak gate (BLUEPRINT § 1): forbidden tokens found in core.")
    print()
    for path, lineno, term, line in findings:
        print(f"  {path}:{lineno}: {term!r}")
        print(f"    {line}")
        print()
    print("Core stays generic. Move overlay-specific names to the overlay package.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
