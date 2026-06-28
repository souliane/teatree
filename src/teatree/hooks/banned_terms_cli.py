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
import sys
import tomllib
from pathlib import Path

from teatree.hooks.banned_terms_tree_scan import BannedTermsUnsetError
from teatree.hooks.term_match import file_matches as _file_matches
from teatree.hooks.term_match import line_matches, strip_emails
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail

# How long to wait for ``git diff`` before treating the staged diff as
# unresolvable and falling back to a full-file scan. A hook that hangs blocks
# the commit, so the budget is deliberately tight.
_GIT_DIFF_TIMEOUT_S = 10

# The REQUIRED term-list key (an unset value fails loud) vs the OPTIONAL
# allow-list key (an unset value defaults to empty). See ``_load_terms`` /
# ``_load_allowlist`` for the split.
_TERMS_KEY = "banned_terms"
_ALLOWLIST_KEY = "banned_terms_allowlist"


def _load_array(config: Path, key: str) -> tuple[str, ...] | None:
    """Return the first ``key`` array found in any TOML section, or ``None`` if unset.

    Mirrors the old shell extractor: scan every top-level section (and the
    document root) for *key* and use the first list found. ``None`` is the
    distinct "unset" signal — the key appears as a list in NO section, or the
    config is unloadable — that the caller turns into a LOUD failure for the
    REQUIRED ``banned_terms`` key while leaving the OPTIONAL allowlist at its
    empty default. An explicit empty array returns ``()`` (set, but
    deliberately empty), never ``None``.
    """
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    for value in [*data.values(), data]:
        if isinstance(value, dict) and key in value:
            entries = value[key]
            if isinstance(entries, list):
                return tuple(str(e).strip() for e in entries if str(e).strip())
    return None


def _load_terms(config: Path) -> tuple[str, ...]:
    """Return the ``banned_terms`` array; RAISE when it is genuinely unset.

    An explicit ``banned_terms = []`` is the operator's deliberate no-terms
    choice and returns ``()``. A genuinely-absent key (or an unloadable config)
    raises :class:`BannedTermsUnsetError` — an unset list is too
    dangerous to scan as empty because a load bug would look identical to a
    deliberate empty list.
    """
    terms = _load_array(config, _TERMS_KEY)
    if terms is None:
        raise BannedTermsUnsetError.for_key(_TERMS_KEY)
    return terms


def _load_allowlist(config: Path) -> tuple[str, ...]:
    """Return the ``banned_terms_allowlist`` carve-out array from the TOML config.

    The allow-list names the company's OWN identifiers (synthetic example:
    ``myorg-engineering`` / ``myorg-product``, internal-URL namespaces) that are
    NEVER a leak — they are the org's own org/repo names, not customer PII. Each
    entry's token-run is removed from a line before banned-term matching, so a
    shorter banned term (a bare org slug) can no longer surface inside a longer
    company-owned identifier. Unlike ``banned_terms`` the allow-list is OPTIONAL:
    an absent key defaults to empty (preserving the prior behaviour), never a
    raise.
    """
    return _load_array(config, _ALLOWLIST_KEY) or ()


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
    parser.add_argument("--config", required=True, help="TOML file with a banned_terms array.")
    parser.add_argument(
        "--diff-only",
        action="store_true",
        help="Scan only the staged diff's added lines per file (pre-commit hook mode), "
        "so a pre-existing committed banned term does not block an unrelated commit.",
    )
    parser.add_argument("files", nargs="*", help="Files to scan.")
    args = parser.parse_args(argv)

    config = Path(args.config).expanduser()
    if not config.is_file():
        return 0  # no config file ⇒ no-op (a machine with no teatree config)
    try:
        terms = _load_terms(config)
    except BannedTermsUnsetError as exc:
        # The config EXISTS but omits banned_terms — fail LOUD (exit 2, the
        # scanner's "could not run" code) rather than silently scan as empty:
        # an unset list is indistinguishable from a load bug. An
        # explicit ``banned_terms = []`` does not raise and is a clean no-op.
        sys.stderr.write(f"{exc}\n")
        return 2
    if not terms:
        return 0  # explicit empty list ⇒ deliberate no-op
    allowlist = _load_allowlist(config)

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
