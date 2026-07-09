"""Pre-commit + CI gate: forbid overlay-scoped names and opaque IDs in core.

BLUEPRINT § 1 ("Core stays generic"): no overlay-specific names appear
in the scanned roots. Per-overlay specifics live in the overlay package
and in the DB-home `ConfigSetting` store (PRIVATE to the operator).

Two passes run over each scanned file.

Pass 1 — configured term list. Forbidden tokens loaded at runtime from
`$TEATREE_OVERLAY_LEAK_TERMS` (comma-separated) or from the DB-home
`overlay_leak_terms` `ConfigSetting` row. The public repo ships with an empty
default; each operator extends it locally. Whole-token matching
(`teatree.hooks.term_match`) is used so a generic word that merely contains
a configured term as a substring does not trigger — the SAME matcher the
`banned_terms` posting gate uses.

Pass 2 — opaque Slack/forge ID (always-on). A real-shaped channel/DM/user/
app/team id (`C0`/`D0`/`U0`/`A0`/`T0`) is an internal reference that carries
no dictionary word, so the term list never caught it. This pass needs no
operator config; a synthetic-placeholder allowlist (`teatree.hooks.opaque_id`)
keeps fixtures/examples from tripping.

The term-list pass is silently inert when neither source is populated. The
`--require-terms` flag (the form CI passes) makes that misconfiguration a
LOUD exit-2 failure; local dev omits the flag and stays green.

Exit codes:

* ``0`` — clean.
* ``1`` — a configured term OR a real-shaped opaque id appears in the scan.
* ``2`` — ``--require-terms`` and no terms are configured (MISCONFIGURED).
"""

import os
import subprocess
import sys
from pathlib import Path

from teatree.config import cold_reader
from teatree.hooks.opaque_id import find_opaque_ids
from teatree.hooks.term_match import matched_term

_FINDINGS_EXIT_CODE = 1
_MISCONFIGURED_EXIT_CODE = 2


def _load_terms() -> tuple[str, ...]:
    """Load forbidden tokens from the env override or the DB-home overlay_leak_terms list.

    ``TEATREE_OVERLAY_LEAK_TERMS`` (comma-separated) wins; otherwise the
    canonical ``overlay_leak_terms`` ``ConfigSetting`` row (read Django-free via
    :mod:`teatree.config.cold_reader`). Empty (default) leaves the term-list pass
    inert — the always-on opaque-ID pass still runs.
    """
    env = os.environ.get("TEATREE_OVERLAY_LEAK_TERMS", "")
    if env:
        return tuple(t.strip() for t in env.split(",") if t.strip())
    terms = cold_reader.list_setting("overlay_leak_terms", default=[])
    return tuple(str(t) for t in terms if isinstance(t, str) and t.strip())


OVERLAY_LEAK_TERMS: tuple[str, ...] = _load_terms()

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


def _scan(paths: list[Path]) -> list[tuple[Path, int, str, str]]:
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
            if OVERLAY_LEAK_TERMS:
                term = matched_term(line, OVERLAY_LEAK_TERMS)
                if term is not None:
                    findings.append((path, lineno, term, line.strip()))
            for opaque in find_opaque_ids(line):
                findings.append((path, lineno, opaque, line.strip()))  # noqa: PERF401 — two sources
    return findings


def main(argv: list[str]) -> int:
    require_terms = "--require-terms" in argv
    file_args = [a for a in argv if not a.startswith("-")]

    if require_terms and not OVERLAY_LEAK_TERMS:
        print(
            "Overlay-leak gate: MISCONFIGURED — term list INERT under --require-terms: "
            "neither TEATREE_OVERLAY_LEAK_TERMS nor the overlay_leak_terms DB row is populated."
        )
        print("Configure the overlay-leak term list so the scan actually guards core.")
        return _MISCONFIGURED_EXIT_CODE

    if not OVERLAY_LEAK_TERMS:
        # Loud inert signal (the opaque-ID pass still runs): never a silent
        # green that hides an unpopulated term list (#1591 sibling).
        print(
            "Overlay-leak gate: WARNING — term list INERT: neither "
            "TEATREE_OVERLAY_LEAK_TERMS nor the overlay_leak_terms DB row is populated "
            "(the opaque-ID pass still runs; populate the term list to guard "
            "overlay names too)."
        )

    if file_args:
        paths = [Path(p) for p in file_args if _is_in_scan_roots(Path(p))]
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
    print("Core stays generic. Move overlay-specific names to the overlay package; scrub opaque IDs.")
    return _FINDINGS_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
