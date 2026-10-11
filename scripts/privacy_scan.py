"""Scan text (typically a git diff) for privacy-sensitive patterns.

Used by: retro (§ Privacy Scan), contribute (§2 Pre-Flight), and the
public-repo pre-push leak gate (``scripts/hooks/refuse-public-push-with-leak.sh``).

Stdlib-only on purpose: the pre-push gate runs this file with whatever Python 3.13+
the host has and ``PYTHONPATH`` at the checkout's ``src``, so a missing third-party
package can never be a reason for the gate not to run.

Exit codes:

* ``0`` — clean (no findings), or ``--no-strict`` regardless of findings.
* :data:`PRIVACY_FINDINGS_EXIT_CODE` (``3``) — genuine findings present in
    strict mode. A DEDICATED code, distinct from the generic Python exception code
    (``1``) and the argparse usage-error code (``2``), so a caller can tell a finding
    from a crash. The pre-push gate refuses on both.
* :data:`REGISTRY_UNAVAILABLE_EXIT_CODE` (``4``) — the banned-term registry is unset,
    empty or unreadable. A scan without a term source proves nothing, so it never
    reports clean.
* :data:`REGISTRY_UNVERIFIED_EXIT_CODE` (``5``) — ``--verify-registry`` found the loaded
    registry different from the fingerprint a repository commits for it, or that
    fingerprint unreadable. The report names a gate, never a digest or a term.

A finding names a registry term by ``class#index``, never by its value.
"""

import argparse
import json
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from teatree.hooks.banned_term_registry import allowlist_terms, load_registry, load_term_refs
from teatree.hooks.banned_terms_cli import resolve_banned_terms
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnreadableError, BannedTermsUnsetError
from teatree.hooks.commit_objects import decode_robust
from teatree.hooks.opaque_id import find_opaque_ids
from teatree.hooks.privacy_diff_comments import scan_diff as _scan_diff_comments
from teatree.hooks.term_fingerprint import FingerprintUnreadableError, unverified_gates
from teatree.hooks.term_match import matched_term
from teatree.hooks.term_refs import TermRefs

_DIFF_DETECTORS = (_scan_diff_comments,)

PRIVACY_FINDINGS_EXIT_CODE = 3
REGISTRY_UNAVAILABLE_EXIT_CODE = 4
REGISTRY_UNVERIFIED_EXIT_CODE = 5
_UNREADABLE_INPUT_EXIT_CODE = 2

# Require a plausible local part: it may contain ``.+-`` internally but
# must *end* in an email-local char (alphanumeric/underscore) immediately
# before ``@``. This drops the decorator/attribute class of false
# positives — ``+@pytest.fixture`` (diff ``+`` as a fake local part),
# ``@app.route``, ``@module.attr`` — where the char before ``@`` is a diff
# marker, whitespace, or absent, while still matching genuine addresses
# whose local part ends in a real char (including a one-char local part).
_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9_]([a-zA-Z0-9_.+-]*[a-zA-Z0-9_])?@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}",
    re.ASCII,
)
_HOME_PATH_RE = re.compile(r"(?:/Users/|/home/)[a-zA-Z0-9_.-]+")
# Every branch must yield a FOUR-octet address. The ``10`` branch carries its own
# second octet because ``10`` alone left it three-wide, so a three-component string
# like a doc section reference or a three-part version string matched as a private
# IP — and a real four-octet address matched only its first three octets. The two
# guards bound the quad against a LONGER dotted run on either side: a preceding dot
# would let a five-part build train donate an inner quad, and a following ``.<digit>``
# the same from the right. A trailing sentence period is not one of them.
_IP_RE = re.compile(r"\b(?<![\d.])(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b(?!\.\d)")
_API_KEY_RE = re.compile(r"\b(?:glpat-|sk-|ghp_|gho_|github_pat_|xoxb-|xoxp-)[a-zA-Z0-9_-]{10,}")
_HOSTNAME_RE = re.compile(r"\b[a-z0-9-]+\.internal\.[a-z]+\b|\b[a-z0-9-]+\.corp\.[a-z]+\b")
_RESERVED_DOMAIN_RE = re.compile(r"(?:^|\.)(?:example\.(?:com|org|net)|example|invalid|test|localhost)$", re.IGNORECASE)

# An SSH git remote — ``git@<host>:<org>/<repo>(.git)`` — is transport
# syntax, never an email/PII. ``_EMAIL_RE`` matches the ``git@<host>``
# prefix, so any test or code carrying a normal SSH remote URL would
# otherwise trip the public-repo privacy gate (a recurring false
# positive). The SSH user is always literally ``git`` and the host is
# followed by ``:<path>`` — a real email never has a ``:path`` after the
# domain — so this is a tight, non-weakening exclusion.
_SSH_GIT_REMOTE_RE = re.compile(r"\bgit@[a-zA-Z0-9.-]+:[A-Za-z0-9._/~-]+")


def _is_reserved_domain(address_or_host: str) -> bool:
    return _RESERVED_DOMAIN_RE.search(address_or_host.rpartition("@")[2]) is not None


def _is_ssh_git_remote(line: str, match: re.Match[str]) -> bool:
    """True when an email match is actually the ``git@host`` of an SSH remote."""
    return any(rm.start() <= match.start() and match.end() <= rm.end() for rm in _SSH_GIT_REMOTE_RE.finditer(line))


# Inline allow-annotation, mirroring gitleaks' ``gitleaks:allow`` idiom.
# A line carrying this literal marker is exempt from all findings — used
# so a repo's own privacy-scanner test fixtures and the gate's own
# documentation examples do not self-block the public-repo privacy gate.
# It exempts only the line it appears on; a real leak on any other line
# is still reported.
_ALLOW_MARKER = "privacy-scan:allow"


@dataclass(frozen=True)
class _Scan:
    terms: tuple[str, ...]
    allowlist: tuple[str, ...]
    refs: TermRefs
    terms_only: bool = False


def _banned_allowlist() -> tuple[str, ...]:
    try:
        return allowlist_terms()
    except BannedTermsUnreadableError:
        return ()


def _masked(match: str, scan: _Scan) -> str:
    """A match that is, or holds, a registry term is reported as that term's ``class#index``, never by value."""
    term = matched_term(match, scan.terms, scan.allowlist)
    return match if term is None else scan.refs.ref(term)


def _generic_findings(line: str) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = [
        ("email", m.group())
        for m in _EMAIL_RE.finditer(line)
        if not _is_reserved_domain(m.group()) and not _is_ssh_git_remote(line, m)
    ]
    findings.extend(("home_path", m.group()) for m in _HOME_PATH_RE.finditer(line))
    findings.extend(("private_ip", m.group()) for m in _IP_RE.finditer(line))
    findings.extend(("api_key", m.group()[:20] + "...") for m in _API_KEY_RE.finditer(line))
    findings.extend(
        ("internal_hostname", m.group()) for m in _HOSTNAME_RE.finditer(line) if not _is_reserved_domain(m.group())
    )
    return findings


def _scan_line(line: str, scan: _Scan) -> list[tuple[str, str]]:
    found = [] if scan.terms_only or _ALLOW_MARKER in line else _generic_findings(line)
    found.extend(("opaque_id", opaque) for opaque in find_opaque_ids(line))
    findings = [(category, _masked(match, scan)) for category, match in found]
    if term := matched_term(line, scan.terms, scan.allowlist):
        findings.append(("banned_term", scan.refs.ref(term)))
    return findings


def _run_diff_detectors(text: str) -> list[dict[str, str | int]]:
    """Run each whole-text diff detector fail-open.

    The diff detectors need the unified-diff structure (file headers + ``+``
    markers), so they run over the whole text rather than per line. A detector that
    raises is cannot-evaluate: it is skipped with a warning and never a finding. The
    registry-term and opaque-id passes do not depend on these detectors.
    """
    findings: list[dict[str, str | int]] = []
    for detector in _DIFF_DETECTORS:
        try:
            hits = detector(text)
        except Exception as exc:  # noqa: BLE001 — fail-open: a crashing detector is cannot-evaluate, never a deny.
            name = getattr(detector, "__name__", repr(detector))
            print(f"privacy scan: detector {name} failed ({type(exc).__name__}) — skipped", file=sys.stderr)
            continue
        findings.extend({"line": lineno, "category": category, "match": match} for lineno, category, match in hits)
    return findings


def _masked_detector_hits(text: str, scan: _Scan) -> list[dict[str, str | int]]:
    hits = _run_diff_detectors(text)
    for hit in hits:
        hit["match"] = _masked(str(hit["match"]), scan)
    return hits


def _scan_text(text: str, scan: _Scan) -> list[dict[str, str | int]]:
    """Per-line + whole-text findings for one text blob: every line is scanned, none is read as a diff."""
    findings: list[dict[str, str | int]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        findings.extend(
            {"line": lineno, "category": category, "match": match} for category, match in _scan_line(line, scan)
        )
    if not scan.terms_only:
        findings.extend(_masked_detector_hits(text, scan))
    return findings


#: A unified-diff hunk header — ``@@ … @@`` (2-way) or ``@@@ … @@@`` (combined,
#: one extra ``@`` per parent). The leading run of ``@`` gives the marker-column
#: width: ``len(run) - 1`` columns precede each hunk-body line.
_HUNK_HEADER_RE = re.compile(r"^(@{2,}) ")


def _is_hunk_body(raw: str, marker_width: int) -> bool:
    """True when ``raw`` is a diff hunk-body line for the current marker width.

    A body line begins with ``marker_width`` marker characters, each one of
    space (context), ``+`` (added) or ``-`` (removed). Anything else (a file
    header, a commit-message line, plain text) is not a body line and ends the
    current hunk.
    """
    return len(raw) >= marker_width and all(char in " +-" for char in raw[:marker_width])


def _diff_scan_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(lineno, content)`` for each diff line the per-line scan should inspect.

    Diff-mode scoping for the push-gate path (souliane/teatree#3681). A unified
    diff's *context* (`` ``) and *removed* (``-``) hunk lines are already-public
    by definition — they exist unchanged on the parent commit the push builds
    onto — so a credential/PII finding on them is a false positive that blocks a
    legitimate push (a refactor pulling synthetic fixtures into the diff as
    context/removed lines). They are skipped. Every other line is yielded
    verbatim: *added* (``+``) hunk lines carry the genuinely-new content the
    gate must still catch, and lines outside any hunk — file headers, or plain
    text piped with no diff structure — are new content too and are scanned
    exactly as in whole-file mode. The push gate hands commit messages over on
    stdin in ``--text`` mode, never in this stream: git puts no separator
    between one commit's last hunk and the next commit's subject.

    Combined diffs (``git show --cc`` on merge commits) carry one marker column
    per parent; a line counts as added when any column is ``+``, which keeps a
    merge's conflict resolutions in scope.
    """
    in_hunk = False
    marker_width = 1
    for lineno, raw in enumerate(text.splitlines(), 1):
        header = _HUNK_HEADER_RE.match(raw)
        if header is not None:
            marker_width = len(header.group(1)) - 1
            in_hunk = marker_width >= 1
            continue
        if in_hunk and _is_hunk_body(raw, marker_width):
            if "+" in raw[:marker_width]:
                yield lineno, raw
            continue
        in_hunk = False
        yield lineno, raw


def _scan_diff(text: str, scan: _Scan) -> list[dict[str, str | int]]:
    """Diff-mode findings: per-line detectors over ADDED lines only, plus whole-text detectors.

    The per-line detectors run over :func:`_diff_scan_lines` (added + non-hunk
    lines), so already-public context/removed lines never trip them. The whole-text
    diff detectors still see the full text — they are already added-line-scoped
    internally.
    """
    findings: list[dict[str, str | int]] = []
    for lineno, line in _diff_scan_lines(text):
        findings.extend(
            {"line": lineno, "category": category, "match": match} for category, match in _scan_line(line, scan)
        )
    if not scan.terms_only:
        findings.extend(_masked_detector_hits(text, scan))
    return findings


#: Directories a directory-argument walk never descends into: version-control
#: internals, vendored dependencies, and tool caches — none of which is source
#: the operator is about to push, and all of which would add noise or churn.
_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
    }
)


def _iter_directory_files(root: Path) -> Iterator[Path]:
    """Yield the regular files under *root*, skipping VCS / vendored / cache dirs and symlinks."""
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if _SKIP_DIR_NAMES & set(path.relative_to(root).parts):
            continue
        yield path


def _scan_directory(root: Path, scan: _Scan) -> list[dict[str, str | int]]:
    """Walk *root* and scan each text file, tagging every finding with its file path.

    A directory is the scanner's most obvious invocation (``privacy-scan .``); it must
    not traceback on it. A binary / undecodable file is skipped, so the scan degrades to
    fewer files, never a crash — and each finding names the file it is in so it is locatable.
    """
    findings: list[dict[str, str | int]] = []
    for path in _iter_directory_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(root).as_posix()
        for finding in _scan_text(text, scan):
            finding["file"] = rel
            findings.append(finding)
    return findings


def _location(finding: dict[str, str | int]) -> str:
    """Locatable position of a finding: ``<file>:N`` in a directory scan, else ``line N``."""
    file = finding.get("file")
    return f"{file}:{finding['line']}" if file else f"line {finding['line']}"


def _plain_summary(findings: list[dict[str, str | int]]) -> str:
    """Deterministic plain-text findings summary for non-TTY callers.

    Stable, greppable, line-oriented (one finding per line: location, category,
    redacted match). Consumed verbatim by the pre-push gate's refusal message and
    by any scripted caller of ``t3 tool privacy-scan``. The redaction of the match
    itself is already applied upstream in ``_scan_line`` (api keys are truncated to
    a 20-char prefix); other categories carry the raw match by design so the user
    can locate the offending text.
    """
    if not findings:
        return "Privacy scan: clean (0 findings)"
    header = f"Privacy scan: {len(findings)} finding(s)"
    rows = [f"  {_location(f)}: {f['category']}: {f['match']}" for f in findings]
    return "\n".join([header, *rows])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan text for privacy-sensitive patterns.")
    parser.add_argument("input_file", nargs="?", default="-", help="File to scan (- for stdin, or a file path)")
    parser.add_argument(
        "--strict",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Strict mode (exit non-zero on any finding). Use --no-strict for warnings only.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output", help="Output as JSON")
    parser.add_argument("--text", action="store_true", help="Scan every line of stdin as prose, not as a unified diff")
    parser.add_argument(
        "--terms-only",
        action="store_true",
        help="Run only the registry-term and opaque-id passes (a whole-history range cannot pass the generic ones)",
    )
    parser.add_argument(
        "--verify-registry",
        type=Path,
        metavar="FINGERPRINT",
        help="Do not scan: check the registry this scan loads against the committed fingerprint file FINGERPRINT",
    )
    return parser


def _verify_registry(fingerprint: Path) -> int:
    """Hold the registry a scan would load against the committed fingerprint; report gates, never digests or terms."""
    try:
        resolve_banned_terms()
        registry = load_registry() or {}
    except BannedTermsUnsetError as exc:
        print(f"privacy scan: cannot verify the term registry — {exc}", file=sys.stderr)
        return REGISTRY_UNAVAILABLE_EXIT_CODE
    try:
        differing = unverified_gates(registry, fingerprint)
    except FingerprintUnreadableError as exc:
        print(f"privacy scan: {exc}", file=sys.stderr)
        return REGISTRY_UNVERIFIED_EXIT_CODE
    if differing:
        print(
            f"privacy scan: the term registry this scan loaded does not match {fingerprint.name} for: "
            f"{', '.join(differing)}",
            file=sys.stderr,
        )
        return REGISTRY_UNVERIFIED_EXIT_CODE
    return 0


def _findings(args: argparse.Namespace, scan: _Scan) -> list[dict[str, str | int]]:
    if args.input_file == "-":
        text = decode_robust(sys.stdin.buffer.read())
        return _scan_text(text, scan) if args.text else _scan_diff(text, scan)
    target = Path(args.input_file)
    if target.is_dir():
        return _scan_directory(target, scan)
    return _scan_text(decode_robust(target.read_bytes()), scan)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.verify_registry is not None:
        return _verify_registry(args.verify_registry)
    try:
        terms = resolve_banned_terms()
    except BannedTermsUnsetError as exc:
        print(f"privacy scan: cannot scan without a term source — {exc}", file=sys.stderr)
        return REGISTRY_UNAVAILABLE_EXIT_CODE
    scan = _Scan(terms, _banned_allowlist(), load_term_refs(terms), terms_only=args.terms_only)
    try:
        all_findings = _findings(args, scan)
    except OSError as exc:
        print(f"privacy scan: cannot read {args.input_file}: {exc.strerror}", file=sys.stderr)
        return _UNREADABLE_INPUT_EXIT_CODE
    all_findings.sort(key=lambda f: (str(f.get("file", "")), int(f["line"])))
    print(json.dumps(all_findings, indent=2) if args.json_output else _plain_summary(all_findings))
    return PRIVACY_FINDINGS_EXIT_CODE if all_findings and args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
