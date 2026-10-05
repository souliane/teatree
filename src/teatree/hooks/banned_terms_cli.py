"""File-scanning CLI for the banned-term hook.

The diff gate reads the classed ``banned_term_registry`` through the shared
resolver. An unset or unreadable registry always fails closed.
"""

import argparse
import os
import sys
from pathlib import Path

from teatree.hooks.banned_terms_tree_scan import BannedTermsUnsetError
from teatree.hooks.term_match import file_matches as _file_matches
from teatree.hooks.term_match import line_matches, matched_term
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail

# How long to wait for ``git diff`` before treating the staged diff as
# unresolvable and falling back to a full-file scan. A hook that hangs blocks
# the commit, so the budget is deliberately tight.
_GIT_DIFF_TIMEOUT_S = 10

# The variables that tell git WHERE the repository is; git exports them to hooks.
_GIT_REPO_LOCATION_VARS = frozenset({"GIT_DIR", "GIT_WORK_TREE"})


def resolve_banned_terms(*, db_path: Path | None = None) -> tuple[str, ...]:
    from teatree.hooks.banned_term_registry import terms_for_gate  # noqa: PLC0415 — cold-path import

    return terms_for_gate("diff", db_path=db_path)


def report_unset(exc: BannedTermsUnsetError) -> int:
    """Refuse to scan when the registry is absent or unreadable."""
    sys.stderr.write(f"{exc}\n")
    return 2


def _load_allowlist(db_path: Path | None = None) -> tuple[str, ...]:
    """Return the registry ``allow`` carve-out array.

    The allow-list names the company's OWN identifiers (synthetic example:
    ``myorg-engineering`` / ``myorg-product``, internal-URL namespaces) that are
    NEVER a leak — they are the org's own org/repo names, not customer PII. Each
    entry's token-run is removed from a line before banned-term matching, so a
    shorter banned term (a bare org slug) can no longer surface inside a longer
    company-owned identifier. The allow-list is optional and defaults to empty.
    """
    from teatree.hooks.banned_term_registry import allowlist_terms  # noqa: PLC0415 — cold-path import

    return allowlist_terms(db_path)


def _git_env_locating_the_repo_from_cwd() -> dict[str, str]:
    """The process env with the git variables that name a repository dropped.

    ``git commit`` exports ``GIT_DIR``/``GIT_WORK_TREE`` to every hook. With
    ``GIT_DIR`` set, git stops discovering the repository from the CWD and takes
    the CWD for the work-tree ROOT, so a relative pathspec resolves against the
    repository root instead — a hook running in a SUBDIRECTORY then reads a
    same-named file from the wrong level, silently leaving the file it was asked
    about unscanned. Dropping both restores CWD-relative resolution.
    """
    return {name: value for name, value in os.environ.items() if name not in _GIT_REPO_LOCATION_VARS}


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
            env=_git_env_locating_the_repo_from_cwd(),
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


def _line_is_own_repo_url_only(
    line: str, terms: tuple[str, ...], allowlist: tuple[str, ...], config_path: Path | None
) -> bool:
    """Return True iff every banned term on ``line`` sits ONLY inside an own private-repo URL.

    A forge work-item URL naming one of the overlay's OWN configured
    ``private_repos`` (``https://host/<org>/<repo>/-/issues/N``) is the
    structurally-required address of that repo's work item, not a customer leak —
    so a line whose banned-term occurrences all sit inside such URLs is
    allow-listed (#3251). The own-repo-URL definition is the SINGLE canonical one
    shared with the posting gate
    (:func:`own_repo_url_carve_out.term_only_inside_own_repo_urls`): a matching
    term whose every occurrence disappears once own-repo URLs are blanked is
    URL-only; the FIRST matching term that survives blanking (a bare term, or a
    term in a FOREIGN URL) short-circuits to False so the line still flags.
    Fail-safe-to-block: with no ``private_repos`` configured no term is URL-only,
    so the line still flags.
    """
    from teatree.hooks.own_repo_url_carve_out import term_only_inside_own_repo_urls  # noqa: PLC0415 — import cycle

    matched_any = False
    for term in terms:
        if matched_term(line, (term,), allowlist) is None:
            continue
        matched_any = True
        if not term_only_inside_own_repo_urls(line, term, config_path=config_path):
            return False
    return matched_any


def _drop_own_repo_url_hits(
    hits: list[tuple[int, str, str]], terms: tuple[str, ...], allowlist: tuple[str, ...], config_path: Path | None
) -> list[tuple[int, str, str]]:
    """Drop every hit whose line's banned terms sit only inside an own private-repo URL (#3251)."""
    return [hit for hit in hits if not _line_is_own_repo_url_only(hit[2], terms, allowlist, config_path)]


def _diff_only_report(
    files: list[str],
    terms: tuple[str, ...],
    repo: Path,
    allowlist: tuple[str, ...] = (),
    config_path: Path | None = None,
) -> list[str]:
    """Build the BANNED TERM report scanning only each file's staged ADDED lines.

    When the staged diff cannot be resolved for a file (``staged_added_lines``
    returns ``None``), fall back to that file's FULL-file scan — failing closed,
    never open. The added-line scan applies the same company-identifier *allowlist*
    carve-out, the own-private-repo-URL
    carve-out (:func:`_line_is_own_repo_url_only`, #3251), and whole-token matcher
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
            hits = _drop_own_repo_url_hits(
                _file_matches(str(path), terms, allowlist=allowlist), terms, allowlist, config_path
            )
            if not hits:
                continue
            report.append(f"BANNED TERM in {file}:")
            report.extend(f"  {line_number}:{line}" for line_number, _term, line in hits)
            continue
        flagged = [
            line
            for line in added
            if line_matches(line, terms, allowlist)
            and not _line_is_own_repo_url_only(line, terms, allowlist, config_path)
        ]
        if not flagged:
            continue
        report.append(f"BANNED TERM in {file}:")
        report.extend(f"  +:{line}" for line in flagged)
    return report


def _full_file_report(files: list[str], terms: tuple[str, ...], allowlist: tuple[str, ...] = ()) -> list[str]:
    """Build the BANNED TERM report scanning each staged file in full.

    The own-private-repo-URL carve-out is applied only on the pre-commit
    ``--diff-only`` path (:func:`_diff_only_report`), NOT here: this full-file
    scan is also the surface the #1415 posting gate shells out to, and that gate
    already applies the SAME own-repo-URL carve-out downstream in
    ``banned_terms/deny.py`` with its own informative "own configured repo" warn —
    carving it out here too would silently suppress that warn (#3251).
    """
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
        # An absent or unreadable registry cannot prove the scan was clean.
        return report_unset(exc)
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
