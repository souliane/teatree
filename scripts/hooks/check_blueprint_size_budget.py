"""Pre-commit + CI hook: BLUEPRINT corpus size budget (#1128, #2040).

The BLUEPRINT is functional + architectural, not a prose mirror of the
code. The budget is a backstop against implementation prose creeping
back into the corpus (the ``skill-prose-ban`` #140 precedent: a rule the
user keeps restating becomes a deterministic gate).

Two-tier enforcement (#2040 — the prior absolute-cap-on-merge-tree gate
red-blocked innocent PRs and forced a treadmill of serialized cap bumps):

Tier 1 — HARD, DELTA-based, race-free. A commit fails only when ITS OWN
diff grows the BLUEPRINT corpus past a per-PR byte allowance, measured
against the merge-base with the base ref — not the absolute merged size.
Concurrent growth of main between branch-point and merge can never red a
PR whose own diff is within allowance. This is the same merge-base/
race-free shape as the §17.1 invariant-numbering gate
(``check_blueprint_invariant_numbering.py``).

Tier 2 — WARN, absolute. When the merged corpus approaches the soft
budget the gate prints a loud "split a section into a linked appendix"
message and exits 0 — never a hard block. Per the binding warn-not-fail
rule for gates that cannot cleanly separate legit growth from bloat, and
the blueprint-cap-may-raise-when-legit rule.

Per-PR delta allowance (bytes) — a single PR may legitimately add a
reviewed section; a runaway prose dump is what the hard gate catches:

- Top-level ``BLUEPRINT.md`` own growth:   8 000
- Combined corpus own growth:             12 000

Soft (warn-only) absolute thresholds (bytes):

- Top-level ``BLUEPRINT.md``:     90 000
- ``docs/blueprint/`` corpus:    116 000
- Combined corpus total:         206 000

BLUEPRINT.md is a SINGLE file by user decision — never split, never
consolidate-by-splitting. The split-to-appendix prompt targets the
``docs/blueprint/`` appendices, not the top-level file.

Escape hatch: ``BLUEPRINT_SIZE_OVERRIDE=1`` skips the hard delta gate.
"""

import argparse
import os
import pathlib
import subprocess
import sys

_TOP_FILE = "BLUEPRINT.md"
_APPENDIX_DIR = "docs/blueprint"

_PER_PR_TOP_LEVEL_DELTA_BYTES = 8_000
_PER_PR_TOTAL_DELTA_BYTES = 12_000

_SOFT_TOP_LEVEL_BYTES = 90_000
_SOFT_APPENDICES_BYTES = 116_000
_SOFT_TOTAL_BYTES = 206_000


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def _size(path: pathlib.Path) -> int:
    return path.stat().st_size if path.is_file() else 0


def _appendix_total(root: pathlib.Path) -> int:
    appendix_dir = root / _APPENDIX_DIR
    if not appendix_dir.is_dir():
        return 0
    return sum(_size(p) for p in appendix_dir.glob("*.md"))


def _git_show_size(repo: pathlib.Path, ref: str, path: str) -> int:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return 0
    return len(result.stdout)


def _git_ls_appendix(repo: pathlib.Path, ref: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", ref, f"{_APPENDIX_DIR}/"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.endswith(".md")]


def _corpus_size_at(repo: pathlib.Path, ref: str) -> tuple[int, int]:
    """Return (top_level_bytes, appendix_bytes) of the corpus at *ref*."""
    top = _git_show_size(repo, ref, _TOP_FILE)
    appendix = sum(_git_show_size(repo, ref, p) for p in _git_ls_appendix(repo, ref))
    return top, appendix


def _merge_base(repo: pathlib.Path, base_ref: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", base_ref, "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def _blueprint_touched(repo: pathlib.Path, base_ref: str) -> bool:
    """True when BLUEPRINT.md or any docs/blueprint/*.md differs vs base.

    FAIL-CLOSED: a git failure (returncode != 0) is NOT a clean "untouched"
    result — it means the gate cannot tell. Treat that as touched (return True)
    and emit a stderr diagnostic so the delta check still runs and the failure
    is visible in CI, rather than silently exiting 0 with no check (#2040).
    """
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", f"{base_ref}...HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(
            f"WARNING: `git diff {base_ref}...HEAD` failed (rc={result.returncode}); "
            "treating BLUEPRINT as touched for safety. stderr: "
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )
        return True
    return any(f == _TOP_FILE or f.startswith(f"{_APPENDIX_DIR}/") for f in result.stdout.splitlines())


def _blueprint_in_commit() -> bool:
    """True when BLUEPRINT.md or an appendix is part of the staged change."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        check=False,
    )
    staged = result.stdout.splitlines()
    return any(f == _TOP_FILE or f.startswith(f"{_APPENDIX_DIR}/") for f in staged)


def _emit_warning(top: int, appendix: int, total: int) -> None:
    warnings: list[str] = []
    if top > _SOFT_TOP_LEVEL_BYTES:
        warnings.append(f"{_TOP_FILE}: {top:,} B over soft budget {_SOFT_TOP_LEVEL_BYTES:,} B")
    if appendix > _SOFT_APPENDICES_BYTES:
        warnings.append(f"{_APPENDIX_DIR}/: {appendix:,} B over soft budget {_SOFT_APPENDICES_BYTES:,} B")
    if total > _SOFT_TOTAL_BYTES:
        warnings.append(f"corpus total: {total:,} B over soft budget {_SOFT_TOTAL_BYTES:,} B")
    if not warnings:
        return
    print(file=sys.stderr)
    print("  BLUEPRINT approaching size budget (#2040 — warn only, not blocking):", file=sys.stderr)
    print(file=sys.stderr)
    for line in warnings:
        print(f"    - {line}", file=sys.stderr)
    print(file=sys.stderr)
    print("  Split a section into a linked appendix under docs/blueprint/ to", file=sys.stderr)
    print("  keep the corpus digestible. BLUEPRINT.md stays one file; move", file=sys.stderr)
    print("  appendix-class detail out, not the top-level architecture.", file=sys.stderr)
    print(file=sys.stderr)


def _emit_delta_failure(top_delta: int, total_delta: int) -> None:
    breaches: list[str] = []
    if top_delta > _PER_PR_TOP_LEVEL_DELTA_BYTES:
        breaches.append(
            f"{_TOP_FILE}: this change adds {top_delta:,} B > per-PR allowance {_PER_PR_TOP_LEVEL_DELTA_BYTES:,} B"
        )
    if total_delta > _PER_PR_TOTAL_DELTA_BYTES:
        breaches.append(
            f"corpus total: this change adds {total_delta:,} B > per-PR allowance {_PER_PR_TOTAL_DELTA_BYTES:,} B"
        )
    print(file=sys.stderr)
    print("  BLUEPRINT per-PR size-delta budget FAILED (#2040):", file=sys.stderr)
    print(file=sys.stderr)
    for line in breaches:
        print(f"    - {line}", file=sys.stderr)
    print(file=sys.stderr)
    print("  This gate measures only THIS change's own growth vs its", file=sys.stderr)
    print("  merge-base, so concurrent main growth never affects it.", file=sys.stderr)
    print("  The BLUEPRINT is architectural, not a prose mirror of the code:", file=sys.stderr)
    print("  move implementation detail to docstrings, --help text, CLAUDE.md,", file=sys.stderr)
    print("  AGENTS.md, or a linked docs/blueprint/ appendix. To bypass for a", file=sys.stderr)
    print("  reviewed bump in the same commit:", file=sys.stderr)
    print("    BLUEPRINT_SIZE_OVERRIDE=1 git commit ...", file=sys.stderr)
    print(file=sys.stderr)


def _evaluate(repo: pathlib.Path, base_ref: str, *, ci: bool) -> int:
    """Delta hard gate + absolute warn, against *base_ref*'s merge-base.

    When *ci* is True the gate is FAIL-CLOSED on an unresolvable merge-base: a
    BLUEPRINT-touching PR whose merge-base can't be computed exits 1 with a
    diagnostic, rather than silently exiting 0 with the delta check never run
    (#2040 fake-green). The pre-commit path (*ci* False) keeps the conservative
    fail-open so a developer's transient git state never blocks a local commit;
    the merge-result tree is re-checked by the CI gate anyway.
    """
    if not _blueprint_touched(repo, base_ref):
        return 0

    head_top = _size(repo / _TOP_FILE)
    head_appendix = _appendix_total(repo)
    head_total = head_top + head_appendix

    _emit_warning(head_top, head_appendix, head_total)

    if os.environ.get("BLUEPRINT_SIZE_OVERRIDE") == "1":
        return 0

    merge_base_sha = _merge_base(repo, base_ref)
    if merge_base_sha is None:
        print(
            f"WARNING: cannot compute merge-base of {base_ref} and HEAD; the per-PR delta check could not run.",
            file=sys.stderr,
        )
        if ci:
            print(
                "  FAIL-CLOSED (--ci): a BLUEPRINT-touching PR with an unresolvable "
                "merge-base reds the gate rather than skipping the check. With "
                "fetch-depth: 0 the merge-base resolves on the happy path, so this "
                "only fires on a genuinely broken base ref / fetch.",
                file=sys.stderr,
            )
            return 1
        return 0

    base_top, base_appendix = _corpus_size_at(repo, merge_base_sha)
    top_delta = head_top - base_top
    total_delta = (head_top + head_appendix) - (base_top + base_appendix)

    if top_delta > _PER_PR_TOP_LEVEL_DELTA_BYTES or total_delta > _PER_PR_TOTAL_DELTA_BYTES:
        _emit_delta_failure(top_delta, total_delta)
        return 1
    return 0


def main() -> int:
    if not _blueprint_in_commit():
        return 0
    return _evaluate(_repo_root(), "origin/main", ci=False)


def ci_main(*, repo: pathlib.Path, base_ref: str) -> int:
    return _evaluate(repo, base_ref, ci=True)


def _cli_entry(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ci", action="store_true", help="Run the delta gate against --base-ref.")
    parser.add_argument("--base-ref", default="origin/main", help="Base ref to diff against (default: origin/main).")
    parser.add_argument("--repo", default=".", help="Repository root (default: cwd).")
    args = parser.parse_args(argv)
    if args.ci:
        return ci_main(repo=pathlib.Path(args.repo).resolve(), base_ref=args.base_ref)
    return main()


if __name__ == "__main__":
    raise SystemExit(_cli_entry(sys.argv[1:]))
