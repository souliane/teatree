"""Pre-commit + CI gate: forbid overlay-scoped names and opaque IDs in core.

BLUEPRINT § 1 ("Core stays generic"): no overlay-specific names appear
in the scanned roots. Per-overlay specifics live in the overlay package
and in the DB-home `ConfigSetting` store (PRIVATE to the operator).

Two passes run over each scanned file.

Pass 1 — configured term list. Forbidden tokens come from the ``overlay`` class
in ``banned_term_registry`` (or ``TEATREE_TERM_REGISTRY``). The public repo ships
with an empty default; each operator extends it locally. Whole-token matching
(`teatree.hooks.term_match`) is used so a generic word that merely contains
a configured term as a substring does not trigger — the SAME matcher the
posting gate uses.

Pass 2 — opaque Slack/forge ID (always-on). A real-shaped channel/DM/user/
app/team id (`C0`/`D0`/`U0`/`A0`/`T0`) is an internal reference that carries
no dictionary word, so the term list never caught it. This pass needs no
operator config; a synthetic-placeholder allowlist (`teatree.hooks.opaque_id`)
keeps fixtures/examples from tripping.

An empty ``overlay`` list refuses the scan before either pass starts.

Exit codes:

* ``0`` — clean.
* ``1`` — a configured term OR a real-shaped opaque id appears in the scan.
* ``2`` — no overlay terms are configured (MISCONFIGURED).
"""

import argparse
import subprocess
import sys
from pathlib import Path

from teatree.hooks.banned_term_registry import terms_for_gate
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnsetError
from teatree.hooks.opaque_id import find_opaque_ids
from teatree.hooks.term_match import matched_term

_FINDINGS_EXIT_CODE = 1
_MISCONFIGURED_EXIT_CODE = 2


def _load_terms() -> tuple[str, ...]:
    """Load the overlay class from the consolidated registry."""
    return terms_for_gate("overlay")


# Roots that must stay generic. Expanded beyond ``src/teatree``/``docs`` to
# every place real leaks lived: skills, agents, top-level docs, tests,
# scripts (#fix6).
SCAN_ROOTS: tuple[str, ...] = (
    "src/teatree",
    "docs",
    "skills",
    "agents",
    "tests",
    "scripts",
)

# Top-level single files scanned in addition to the directory roots.
SCAN_FILES: tuple[str, ...] = (
    "README.md",
    "BLUEPRINT.md",
    "AGENTS.md",
)

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

# The opaque-ID detector's OWN tests necessarily carry real-shaped ids to
# prove the gate trips — exempting them by path lets the rule document
# itself without self-tripping (the same convention terminology_gate uses).
# The per-line ``leak-scan:allow`` marker (teatree.hooks.opaque_id) covers
# any one-off legitimate example elsewhere.
_EXEMPT_PATH_SUFFIXES: tuple[str, ...] = (
    "tests/teatree_hooks/test_opaque_id.py",
    "tests/test_no_overlay_leak_hook.py",
    "tests/test_privacy_scan_script.py",
)


def _path_is_exempt(path: Path) -> bool:
    posix = path.as_posix()
    return any(posix.endswith(suffix) for suffix in _EXEMPT_PATH_SUFFIXES)


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
        if root_path.is_dir():
            paths.extend(p for p in root_path.rglob("*") if p.is_file())
    paths.extend(Path(f) for f in SCAN_FILES if Path(f).is_file())
    return paths


def _is_in_scan_roots(path: Path) -> bool:
    return any(str(path).startswith(f"{root}/") for root in SCAN_ROOTS) or str(path) in SCAN_FILES


def _scan(paths: list[Path], terms: tuple[str, ...]) -> list[tuple[Path, int, str, str]]:
    """Scan *paths* for configured terms AND always-on opaque ids."""
    findings: list[tuple[Path, int, str, str]] = []
    for path in paths:
        if path.suffix not in TEXT_SUFFIXES:
            continue
        if not path.is_file():
            continue
        if _path_is_exempt(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            term = matched_term(line, terms)
            if term is not None:
                findings.append((path, lineno, term, line.strip()))
            for opaque in find_opaque_ids(line):
                findings.append((path, lineno, opaque, line.strip()))  # noqa: PERF401 — two sources
    return findings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Refuse overlay-specific names in core.")
    parser.add_argument("files", nargs="*")
    file_args = parser.parse_args(argv).files

    try:
        terms = _load_terms()
    except BannedTermsUnsetError as exc:
        print(f"Overlay-leak gate: MISCONFIGURED — {exc}")
        return _MISCONFIGURED_EXIT_CODE
    if not terms:
        print("Overlay-leak gate: MISCONFIGURED — the overlay class in banned_term_registry is empty.")
        print("Configure the overlay-leak term list so the scan actually guards core.")
        return _MISCONFIGURED_EXIT_CODE

    if file_args:
        paths = [Path(p) for p in file_args if _is_in_scan_roots(Path(p))]
    else:
        # Pre-commit invocation without args + CI invocation: walk the tree.
        staged = [p for p in _staged_files() if _is_in_scan_roots(p)]
        paths = staged or _walk_roots()

    findings = _scan(paths, terms)
    if not findings:
        return 0

    print("Overlay-leak gate (BLUEPRINT § 1): forbidden tokens found in core.")
    print()
    for path, lineno, term, line in findings:
        print(f"  {path}:{lineno}: {term!r}")
        print(f"    {line}")
        print()
    print("Core stays generic. Move overlay-specific names to the overlay package; scrub opaque IDs.")
    return _FINDINGS_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
