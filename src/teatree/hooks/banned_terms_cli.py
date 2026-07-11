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

The term list is DB-home: it is read from the canonical ``ConfigSetting`` store
via the Django-free :mod:`teatree.config.cold_reader` (the DB is PRIVATE to the
operator). The ``T3_BANNED_TERMS`` env value (comma-separated) still WINS over
the DB. Set the list with
``t3 <overlay> config_setting set banned_terms '["acme","globex"]'``::

    python -m teatree.hooks.banned_terms_cli <file> [<file> ...]

- exit 0: no file contains a banned term (or an explicit empty list ⇒ no-op).
- exit 1: at least one file contains a banned term. The same
``BANNED TERM in <file>:`` report the shell hook printed is emitted, so the
``banned_terms_scanner`` report parser keeps working unchanged.
- exit 2: the term list is genuinely UNSET (no ``banned_terms`` row AND no env
value) — fail LOUD rather than silently scan as empty, since an unset list is
indistinguishable from a load bug. An explicit ``banned_terms = []`` is the
deliberate no-op (exit 0), not an unset.

The email carve-out lives in ``term_match`` so it, too, is shared rather than
duplicated.

``--diff-only`` scopes the scan to the staged DIFF's ADDED lines per file (the
pre-commit hook entry passes it). Without it, the whole file is scanned — the
mode the posting gate (``banned_terms_scanner`` writes the body to a temp file
and scans it whole) and the parity meta-test rely on. The diff-only mode fixes
the #1415 over-block: staging a one-line edit to a file that ALREADY carries a
committed banned term used to block the commit on the untouched committed line.
The pre-push public-leak gate (``refuse-public-push-with-leak.sh``) re-scans
commit messages before they reach a public remote, so a pre-existing committed
term is still caught before it leaves the machine.
"""

import argparse
import os
import sys
from pathlib import Path

from teatree.config import cold_reader
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnsetError
from teatree.hooks.term_match import file_matches as _file_matches
from teatree.hooks.term_match import line_matches, strip_emails
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail

# How long to wait for ``git diff`` before treating the staged diff as
# unresolvable and falling back to a full-file scan. A hook that hangs blocks
# the commit, so the budget is deliberately tight.
_GIT_DIFF_TIMEOUT_S = 10

# The REQUIRED term-list key: an unset value fails loud (the optional allowlist
# carve-out is read via ``banned_term_registry.allowlist_terms``, empty when unset).
_TERMS_KEY = "banned_terms"

# The env override (comma-separated) that WINS over the DB — the
# secret-from-CI-secret path where the DB row is not populated.
_TERMS_ENV = "T3_BANNED_TERMS"


def _db_array(key: str, db_path: Path | None) -> tuple[str, ...] | None:
    """Return the DB-home ``key`` list, or ``None`` when the row is genuinely UNSET.

    ``None`` is the distinct "unset" signal — no ``key`` row, or a wrong-typed
    value — that the caller turns into a LOUD failure for the REQUIRED
    ``banned_terms`` key while leaving the OPTIONAL allowlist at its empty
    default. An explicit empty list returns ``()`` (set, but deliberately empty),
    never ``None``. Reads the canonical ``ConfigSetting`` store via the
    Django-free :mod:`teatree.config.cold_reader`; *db_path* overrides the DB
    path (else the canonical DB / ``T3_CONFIG_DB``).
    """
    raw = cold_reader.read_setting(key, db_path=db_path)
    if not isinstance(raw, list):
        return None
    return tuple(str(e).strip() for e in raw if str(e).strip())


def resolve_banned_terms(
    config_path: Path | None = None, *, env_value: str = "", db_path: Path | None = None
) -> tuple[str, ...]:
    """Resolve the canonical banned-terms list for a fail-closed scanner.

    The single source-resolution every banned-terms scanner shares, so they
    cannot diverge on WHERE the term list comes from. Resolution order:
    ``T3_BANNED_TERMS`` env override → the consolidated ``banned_term_registry``
    (its diff-gate classes) → the legacy DB-home ``banned_terms`` row.

    A non-empty *env_value* (or the ``T3_BANNED_TERMS`` process env) wins,
    comma-split — the CI-secret path stays authoritative through the registry
    transition. Else the consolidated registry when it is present (dual-read,
    ``banned_term_registry``); else the ``banned_terms`` DB list, which RAISES
    :class:`BannedTermsUnsetError` when BOTH the registry and the row are unset —
    the fail-closed signal that an unreadable source must never silently degrade
    to an empty ban list. An explicit ``banned_terms = []`` is a deliberate no-op
    and returns ``()``.

    *config_path* is accepted for the legacy pre-DB caller (``scripts/privacy_scan``)
    and is not consulted — the term list is DB-home now. *db_path* overrides the
    DB path (else the canonical DB / ``T3_CONFIG_DB``).
    """
    del config_path  # legacy pre-DB arg; the term list is DB-home now
    env = env_value if env_value.strip() else os.environ.get(_TERMS_ENV, "")
    if env.strip():
        return tuple(t.strip() for t in env.split(",") if t.strip())
    from teatree.hooks.banned_term_registry import registry_terms_for_gate  # noqa: PLC0415  dual-read cycle

    registry_terms = registry_terms_for_gate("diff", db_path=db_path)
    if registry_terms is not None:
        return registry_terms
    terms = _db_array(_TERMS_KEY, db_path)
    if terms is None:
        raise BannedTermsUnsetError.for_key(_TERMS_KEY, _TERMS_ENV)
    return terms


def _load_allowlist(db_path: Path | None = None) -> tuple[str, ...]:
    """Return the DB-home ``banned_terms_allowlist`` carve-out array.

    The allow-list names the company's OWN identifiers (synthetic example:
    ``myorg-engineering`` / ``myorg-product``, internal-URL namespaces) that are
    NEVER a leak — they are the org's own org/repo names, not customer PII. Each
    entry's token-run is removed from a line before banned-term matching, so a
    shorter banned term (a bare org slug) can no longer surface inside a longer
    company-owned identifier. Unlike ``banned_terms`` the allow-list is OPTIONAL:
    an absent row defaults to empty (preserving the prior behaviour), never a
    raise. Dual-read: the consolidated ``banned_term_registry`` ``allow`` class
    when present, else the legacy ``banned_terms_allowlist`` row. Reads the
    canonical ``ConfigSetting`` store via :mod:`teatree.config.cold_reader`.
    """
    from teatree.hooks.banned_term_registry import allowlist_terms  # noqa: PLC0415  dual-read cycle

    return allowlist_terms(db_path)


def staged_added_lines(repo: Path, file: str) -> list[str] | None:
    """Return *file*'s ADDED lines from the staged diff, or ``None`` on failure.

    Runs ``git diff --cached -U0 --diff-filter=ACMR -- <file>`` from *repo* and
    keeps the body of each ``+`` line inside a hunk (``-U0`` emits no context
    lines). An EMPTY list means the file has no staged additions; ``None`` is
    the distinct sentinel for "could not resolve the staged diff" (not a git
    repo, git missing, a non-zero exit, a timeout) so the caller can fall back
    to a full-file scan and NEVER fail open on a security gate.

    The extraction is HUNK-AWARE, not prefix-matching. The ``--- ``/``+++ ``
    file headers appear exactly once per file, BEFORE the first ``@@`` hunk
    header; ADDED content lines only appear inside a hunk body. A naive
    ``not line.startswith("+++")`` filter would silently drop a real added
    content line whose own text begins with ``++`` — git renders that as the
    add-marker ``+`` plus ``++text`` = ``+++text`` — so a banned term staged on
    such a line would slip the commit gate (fail-open diff-evasion). Tracking
    hunk state instead keeps ``++text``/``+++text``/``+++ text`` content lines
    (they live in a hunk body) while never seeing the ``+++ b/<file>`` header
    (it is pre-hunk).
    """
    try:
        result = run_allowed_to_fail(
            ["git", "diff", "--cached", "-U0", "--diff-filter=ACMR", "--", file],
            expected_codes=(0,),
            cwd=repo,
            timeout=_GIT_DIFF_TIMEOUT_S,
        )
    except (CommandFailedError, TimeoutExpired, OSError):
        return None
    added: list[str] = []
    in_hunk = False
    for line in result.stdout.splitlines():
        if line.startswith("diff --git"):
            in_hunk = False  # back to per-file headers; ``+++ b/<file>`` is pre-hunk
        elif line.startswith("@@"):
            in_hunk = True  # hunk body begins; subsequent ``+`` lines are added content
        elif in_hunk and line.startswith("+"):
            added.append(line[1:])
    return added


def _diff_only_report(
    files: list[str], terms: tuple[str, ...], repo: Path, allowlist: tuple[str, ...] = ()
) -> list[str]:
    """Build the BANNED TERM report scanning only each file's staged ADDED lines.

    When the staged diff cannot be resolved for a file (``staged_added_lines``
    returns ``None``), fall back to that file's FULL-file scan — failing closed,
    never open. The added-line scan applies the same per-line email carve-out,
    the company-identifier *allowlist* carve-out, and whole-token matcher
    (:mod:`teatree.hooks.term_match`) the full scan uses, so the two paths agree
    on every line they both see.
    """
    report: list[str] = []
    for file in files:
        path = Path(file)
        added = staged_added_lines(repo, file)
        if added is None:
            if not path.is_file():
                continue
            hits = _file_matches(str(path), terms, allowlist=allowlist)
            if not hits:
                continue
            report.append(f"BANNED TERM in {file}:")
            report.extend(f"  {line_number}:{line}" for line_number, _term, line in hits)
            continue
        flagged = [line for line in added if line_matches(strip_emails(line), terms, allowlist)]
        if not flagged:
            continue
        report.append(f"BANNED TERM in {file}:")
        report.extend(f"  +:{line}" for line in flagged)
    return report


def _full_file_report(files: list[str], terms: tuple[str, ...], allowlist: tuple[str, ...] = ()) -> list[str]:
    """Build the BANNED TERM report scanning each staged file in full."""
    report: list[str] = []
    for file in files:
        path = Path(file)
        if not path.is_file():
            continue
        hits = _file_matches(str(path), terms, allowlist=allowlist)
        if not hits:
            continue
        report.append(f"BANNED TERM in {file}:")
        report.extend(f"  {line_number}:{line}" for line_number, _term, line in hits)
    return report


def main(argv: list[str]) -> int:  # pragma: no cover — CLI entry point (orchestrates tested helpers)
    parser = argparse.ArgumentParser(description="Reject files containing banned terms.")
    parser.add_argument(
        "--diff-only",
        action="store_true",
        help="Scan only the staged diff's added lines per file (pre-commit hook mode), "
        "so a pre-existing committed banned term does not block an unrelated commit.",
    )
    parser.add_argument("files", nargs="*", help="Files to scan.")
    args = parser.parse_args(argv)

    try:
        terms = resolve_banned_terms()
    except BannedTermsUnsetError as exc:
        # The term list is genuinely UNSET (no banned_terms row AND no env) —
        # fail LOUD (exit 2, the scanner's "could not run" code) rather than
        # silently scan as empty: an unset list is indistinguishable from a load
        # bug. An explicit ``banned_terms = []`` does not raise and is a no-op.
        sys.stderr.write(f"{exc}\n")
        return 2
    if not terms:
        return 0  # explicit empty list ⇒ deliberate no-op
    allowlist = _load_allowlist()

    if args.diff_only:
        report = _diff_only_report(args.files, terms, Path.cwd(), allowlist)
    else:
        report = _full_file_report(args.files, terms, allowlist)

    if report:
        report.extend(("", f"Banned terms: {','.join(terms)}", "These terms must not appear in this repo."))
        sys.stdout.write("\n".join(report) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
