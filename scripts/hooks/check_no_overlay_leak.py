"""Pre-commit + CI gate: forbid customer/tenant/product names in core.

BLUEPRINT § 1 ("Core stays generic"): no overlay-specific names appear
in `src/teatree/` or `docs/`. Per-overlay specifics live in the overlay
package and in `~/.teatree.toml`.

This gate scans staged files (when run as a pre-commit hook with file
paths) or the full tree (when run in CI without arguments) for tokens
listed in `OVERLAY_LEAK_TERMS`. The list intentionally errs on the side
of strict matching — every term is a real overlay project, customer, or
tenant name that has appeared in past leaks. Add to it whenever a new
overlay registers; remove only when the term is no longer overlay-scoped.

Word-boundary matching is used so generic substrings (e.g. "operate",
"cooperative") do not trigger. Tokens like "t3-oper" are matched as a
whole.

Exit code: 0 if clean, 1 if any term appears in the scanned files.
"""

import re
import subprocess
import sys
from pathlib import Path

# Overlay project, customer, and tenant tokens that must not appear in
# `src/teatree/` or `docs/`. Each entry is matched as a word (with
# `\b` boundaries) and case-insensitively. When a new overlay is
# registered with teatree, append its top-level name + any tenant labels
# the overlay carries.
OVERLAY_LEAK_TERMS: tuple[str, ...] = (
    # Personal dogfood overlays
    "t3-oper",
    "oper-product",
    "oper-skills",
    "oper-e2e",
    "t3-oper-e2e",
    # Customer / tenant labels that have leaked from overlays in the past
    "finporta",
    "atruvia",
    "atplaywright",
    "wuestenrot",
    "home-savings",
    "goerlich",
    "sparkasse",
)

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


def _term_variants(term: str) -> set[str]:
    """Generate kebab/snake/camel/Pascal variants of *term*.

    For a multi-word term like ``home-savings`` returns
    ``{"home-savings", "home_savings", "homeSavings", "HomeSavings"}``.
    Single-word terms produce only the original token.
    """
    parts = re.split(r"[-_]", term)
    variants: set[str] = {term}
    if len(parts) <= 1:
        return variants
    variants.add("-".join(parts))
    variants.add("_".join(parts))
    variants.add(parts[0] + "".join(p.capitalize() for p in parts[1:]))
    variants.add("".join(p.capitalize() for p in parts))
    return variants


def _build_pattern() -> re.Pattern[str]:
    all_variants: set[str] = set()
    for term in OVERLAY_LEAK_TERMS:
        all_variants.update(_term_variants(term))
    escaped_variants = [re.escape(v) for v in all_variants]
    escaped_variants.sort(key=len, reverse=True)
    escaped = "|".join(escaped_variants)
    return re.compile(rf"\b({escaped})\b", re.IGNORECASE)


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
    pattern = _build_pattern()
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
            findings.extend((path, lineno, match.group(0), line.strip()) for match in pattern.finditer(line))
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
