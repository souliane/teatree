"""Pre-commit hook: enforce module-level architectural health.

Checks staged Python files for:
- Files exceeding a LOC threshold (default 500)
- Too many module-level functions (default 10) — prefer methods on classes
- untyped ``dict``-of-``object`` annotations — prefer typed dataclasses/TypedDict

Files already over the cap at HEAD are grandfathered but ratcheted: they may
only SHRINK. A commit that grows an over-cap file (LOC or public-function
count) is blocked, so a god-module can never re-accrete after a split (#1983).

Scope is the first-party Python tree: ``src/`` plus the ``hooks/`` and
``scripts/`` hook/tool code (``_FIRST_PARTY_PREFIXES``). Tests and
third-party/vendored paths are excluded. One shared predicate (``_is_first_party``)
gates both the staged-mode and diff-mode file scans so the two never diverge.

Two entry paths share one ratchet predicate. The default (no args) path runs
per-commit over staged files (``git diff --cached``) — the prek commit hook,
where a merge commit is exempt because a merge brings both parents' growth into
one commit and the per-commit comparison would false-flag it (#1983 exemption).
The ``--from-ref <base>`` path runs over the PR's whole ``base..HEAD`` range —
the bypass-proof CI twin (#2010); the range is computed once base-to-head, NOT
per-commit, so the merge-commit false-positive the staged-mode exemption works
around never arises and no merge exemption is needed in this mode.

See: souliane/teatree codebase audit findings
"""

import argparse
import ast
import pathlib
import re

from teatree.utils.run import run_allowed_to_fail

MAX_LOC = 500
MAX_MODULE_FUNCTIONS = 10

_RENAME_FIELDS = 2
_SINGLE_PATH_FIELD = 1

# First-party Python the ratchet scans. ``src/`` is the package; ``hooks/`` and
# ``scripts/`` are the first-party hook/tool code — where the 6388-LOC
# ``hooks/scripts/hook_router.py`` lives, the single largest first-party file,
# previously invisible to the gate while small ``src/`` modules were forced to
# split. One shared predicate keeps staged-mode and diff-mode scope identical.
_FIRST_PARTY_PREFIXES = ("src/", "hooks/", "scripts/")

# Auto-generated Django migrations are exempt from the module-health caps: a
# squashed ``0001_initial`` legitimately captures a whole app's schema in one
# file and cannot be "split by concern" (Django runs a migration as one unit).
# This mirrors their exclusion from coverage, ruff E501, and the jscpd scan.
_MIGRATIONS_SEGMENT = "/migrations/"


def _is_first_party(path: str) -> bool:
    return path.startswith(_FIRST_PARTY_PREFIXES) and _MIGRATIONS_SEGMENT not in path


# Assembled from a suffix constant rather than spelled out, so this detector —
# which is itself first-party code the module-health scan reads — never flags
# its OWN pattern definitions or messages as offending annotations (#3312).
_OBJECT_SUFFIX = "object]"
_DICT_OBJECT_PATTERNS = [
    f"dict[str, {_OBJECT_SUFFIX}",
    f"Dict[str, {_OBJECT_SUFFIX}",
]


def _is_merge_commit() -> bool:
    result = run_allowed_to_fail(
        ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        expected_codes=None,
    )
    return result.returncode == 0


def _staged_python_files() -> list[str]:
    result = run_allowed_to_fail(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "--", "*.py"],
        expected_codes=None,
    )
    return [f for f in result.stdout.strip().splitlines() if _is_first_party(f)]


def _head_paths() -> dict[str, str]:
    """Map each staged path to its pre-rename path at HEAD.

    A renamed file (``R``) compares against its source path at HEAD so the
    grandfather/ratchet logic follows the move instead of treating the new
    path as a fresh over-cap file. Non-renamed files map to themselves.
    """
    result = run_allowed_to_fail(
        ["git", "diff", "--cached", "--name-status", "-M", "--diff-filter=ACMR", "--", "*.py"],
        expected_codes=None,
    )
    mapping: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        status, *paths = line.split("\t")
        if status.startswith("R") and len(paths) == _RENAME_FIELDS:
            old_path, new_path = paths
            mapping[new_path] = old_path
        elif len(paths) == _SINGLE_PATH_FIELD:
            mapping[paths[0]] = paths[0]
    return mapping


def _count_loc(filepath: str) -> int:
    try:
        with pathlib.Path(filepath).open(encoding="utf-8") as f:
            return sum(1 for line in f if line.strip() and not line.strip().startswith("#"))
    except OSError:
        return 0


def _show_at_ref(filepath: str, ref: str) -> str | None:
    result = run_allowed_to_fail(
        ["git", "show", f"{ref}:{filepath}"],
        expected_codes=None,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _count_loc_at_ref(filepath: str, ref: str) -> int:
    source = _show_at_ref(filepath, ref)
    if source is None:
        return 0
    return sum(1 for line in source.splitlines() if line.strip() and not line.strip().startswith("#"))


def _count_module_level_functions_at_ref(filepath: str, ref: str) -> list[str]:
    source = _show_at_ref(filepath, ref)
    if source is None:
        return []
    return _public_module_functions(source)


def _count_loc_at_head(filepath: str) -> int:
    return _count_loc_at_ref(filepath, "HEAD")


def _count_module_level_functions_at_head(filepath: str) -> list[str]:
    return _count_module_level_functions_at_ref(filepath, "HEAD")


def _public_module_functions(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [
        node.name
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")
    ]


def _count_module_level_functions(filepath: str) -> list[str]:
    try:
        source = pathlib.Path(filepath).read_text(encoding="utf-8")
    except OSError:
        return []
    return _public_module_functions(source)


def _added_line_numbers(filepath: str, head_path: str) -> set[int] | None:
    """Return the set of line numbers added/modified in the staged version, or None for new files."""
    paths = [head_path, filepath] if head_path != filepath else [filepath]
    result = run_allowed_to_fail(
        ["git", "diff", "--cached", "-U0", "-M", "--", *paths],
        expected_codes=None,
    )
    if not result.stdout:
        return None
    added: set[int] = set()
    for match in re.finditer(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", result.stdout):
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) else 1
        added.update(range(start, start + count))
    return added


def _find_dict_object_annotations(filepath: str) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    try:
        with pathlib.Path(filepath).open(encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                stripped = line.strip()
                # Skip type alias definitions — they ARE the fix
                if stripped.startswith("type ") and "=" in stripped:
                    continue
                for pattern in _DICT_OBJECT_PATTERNS:
                    if pattern in line:
                        findings.append((line_num, line.strip()))
                        break
    except OSError:
        pass
    return findings


def _file_violations(filepath: str, prev_loc: int, prev_funcs: list[str], added_lines: set[int] | None) -> list[str]:
    """Apply the shrink ratchet to one file's current vs previous state.

    ``prev_loc`` / ``prev_funcs`` are the file's measurements at the comparison
    baseline (HEAD for staged-mode, the base ref for diff-mode); the current
    side always reads the working tree, which is the PR head in both contexts.
    """
    violations: list[str] = []

    loc = _count_loc(filepath)
    if loc > MAX_LOC:
        if prev_loc <= MAX_LOC:
            violations.append(f"  {filepath}: {loc} LOC (max {MAX_LOC}). Split by concern.")
        elif loc > prev_loc:
            violations.append(
                f"  {filepath}: {loc} LOC, up from {prev_loc} (over the {MAX_LOC} cap). "
                f"Over-cap files may only shrink — split by concern or move code out."
            )

    public_functions = _count_module_level_functions(filepath)
    if len(public_functions) > MAX_MODULE_FUNCTIONS:
        names = ", ".join(public_functions[:5])
        if len(prev_funcs) <= MAX_MODULE_FUNCTIONS:
            violations.append(
                f"  {filepath}: {len(public_functions)} public module-level functions "
                f"(max {MAX_MODULE_FUNCTIONS}). Move to a class. Examples: {names}"
            )
        elif len(public_functions) > len(prev_funcs):
            violations.append(
                f"  {filepath}: {len(public_functions)} public module-level functions, "
                f"up from {len(prev_funcs)} (over the {MAX_MODULE_FUNCTIONS} cap). "
                f"Over-cap files may only shrink — move a function to a class. Examples: {names}"
            )

    for line_num, _line in _find_dict_object_annotations(filepath):
        if added_lines is None or line_num in added_lines:
            violations.append(
                f"  {filepath}:{line_num}: dict[str, {_OBJECT_SUFFIX} — use a dataclass or TypedDict instead"
            )

    return violations


def _merge_base(base_ref: str) -> str:
    result = run_allowed_to_fail(
        ["git", "merge-base", base_ref, "HEAD"],
        expected_codes=None,
    )
    sha = result.stdout.strip()
    return sha or base_ref


def _range_python_files(merge_base: str) -> list[str]:
    result = run_allowed_to_fail(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{merge_base}..HEAD", "--", "*.py"],
        expected_codes=None,
    )
    return [f for f in result.stdout.strip().splitlines() if _is_first_party(f)]


def _range_paths(merge_base: str) -> dict[str, str]:
    """Map each changed path in ``merge_base..HEAD`` to its pre-change path at the merge-base."""
    result = run_allowed_to_fail(
        ["git", "diff", "--name-status", "-M", "--diff-filter=ACMR", f"{merge_base}..HEAD", "--", "*.py"],
        expected_codes=None,
    )
    mapping: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        status, *paths = line.split("\t")
        if status.startswith("R") and len(paths) == _RENAME_FIELDS:
            old_path, new_path = paths
            mapping[new_path] = old_path
        elif len(paths) == _SINGLE_PATH_FIELD:
            mapping[paths[0]] = paths[0]
    return mapping


def _range_added_line_numbers(filepath: str, base_path: str, merge_base: str) -> set[int] | None:
    paths = [base_path, filepath] if base_path != filepath else [filepath]
    result = run_allowed_to_fail(
        ["git", "diff", "-U0", "-M", f"{merge_base}..HEAD", "--", *paths],
        expected_codes=None,
    )
    if not result.stdout:
        return None
    added: set[int] = set()
    for match in re.finditer(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", result.stdout):
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) else 1
        added.update(range(start, start + count))
    return added


def run_diff_mode(base_ref: str) -> int:
    """Ratchet the whole ``base_ref..HEAD`` range (the bypass-proof CI twin, #2010).

    Resolves the merge-base of ``base_ref`` and ``HEAD`` ONCE and uses it as the
    single comparison reference for both the changed-file diff AND the baseline
    LOC/function reads. Reading the baseline at the literal ``base_ref`` tip while
    diffing three-dot (merge-base relative) disagrees when main has diverged and
    independently edited a file — main's own edits then get mis-attributed to the
    branch and a legitimate ratchet-compliant shrink false-blocks.

    The PR diff is taken once over the range, NOT per-commit, so the
    merge-commit false-positive the staged-mode exemption guards against never
    arises here — no merge exemption is needed in this mode.
    """
    merge_base = _merge_base(base_ref)
    base_paths = _range_paths(merge_base)
    violations: list[str] = []
    for filepath in _range_python_files(merge_base):
        base_path = base_paths.get(filepath, filepath)
        violations.extend(
            _file_violations(
                filepath,
                _count_loc_at_ref(base_path, merge_base),
                _count_module_level_functions_at_ref(base_path, merge_base),
                _range_added_line_numbers(filepath, base_path, merge_base),
            )
        )
    return _report(violations)


def _run_staged() -> int:
    if _is_merge_commit():
        return 0

    files = _staged_python_files()
    if not files:
        return 0

    head_paths = _head_paths()
    violations: list[str] = []
    for filepath in files:
        head_path = head_paths.get(filepath, filepath)
        violations.extend(
            _file_violations(
                filepath,
                _count_loc_at_head(head_path),
                _count_module_level_functions_at_head(head_path),
                _added_line_numbers(filepath, head_path),
            )
        )
    return _report(violations)


def _report(violations: list[str]) -> int:
    if not violations:
        return 0
    print("Module health violations:")
    print()
    for v in violations:
        print(v)
    print()
    print(
        "Fix these before committing. Split the file by concern, move\n"
        f"module-level functions to a class, or replace dict[str, {_OBJECT_SUFFIX}\n"
        "with a typed dataclass / TypedDict. There is no bypass — refactor\n"
        "before the commit lands."
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-ref",
        default=None,
        help="Run the ratchet over the whole <ref>..HEAD range (CI twin) instead of staged files.",
    )
    # Tolerated, ignored: prek's commit-msg stage passes the message file path.
    parser.add_argument("ignored_commit_msg_file", nargs="?", default=None)
    args = parser.parse_args(argv)
    if args.from_ref is not None:
        return run_diff_mode(args.from_ref)
    return _run_staged()


if __name__ == "__main__":
    raise SystemExit(main())
