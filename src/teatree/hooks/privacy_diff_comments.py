"""``code_comment_self_reference`` detector for the diff privacy-scanner.

The diff privacy-scanner (``scripts/privacy_scan.py`` →
``t3 tool privacy-scan``, wired into the pre-push gate
``scripts/hooks/refuse-public-push-with-leak.sh``) scans the pushed diff
for emails / keys / IPs / banned terms. This module extends it with a
new, separately-named detector for a recurring leak class the prose rule
keeps missing: **bookkeeping self-references left in code comments** on
added lines — a "consolidated into <bang-MR-ref>" note, a workstream-number
tag, a tracker-id reference, a "per pentest" process aside.

Why a dedicated diff-aware pass rather than another per-line regex in
``_scan_line``: the match must be scoped to (a) **added** lines only, (b)
the file language's **comment** syntax, and (c) **non-markdown/docs**
files. None of that is visible to a stateless per-line scan — it requires
the unified-diff structure (``+++ b/<path>`` file headers, ``+`` add
markers). So this module parses the diff itself.

It deliberately does NOT flag the bare words ``workstream`` / ``umbrella``
— those are legitimate architecture vocabulary in this codebase's own
comments (e.g. "the HEAD/workstream attestation binding", "the
umbrella-protection convention"). The bookkeeping forms that ARE
unambiguous self-references — a workstream *number* (``W20``), a
``sub-ticket`` reference, an MR/PR/ticket id — are what the patterns
target.
"""

import re

# Inline allow-annotation, shared with ``privacy_scan.py``. A comment line
# carrying this literal marker is exempt (used by the scanner's own
# fixtures and doc examples).
_ALLOW_MARKER = "privacy-scan:allow"

CATEGORY = "code_comment_self_reference"

# Self-referential bookkeeping patterns. Each is an unambiguous reference
# to an MR/PR, a ticket/workstream-number, or process narration — never a
# plain architecture term. Matched case-insensitively against the COMMENT
# portion of an added line only.
_SELF_REF_PATTERNS: tuple[re.Pattern[str], ...] = (
    # MR/PR references.
    re.compile(r"!\d{3,4}\b"),
    re.compile(r"\bMR\s+!"),
    re.compile(r"\bmerge request\b", re.IGNORECASE),
    re.compile(r"\bPR\s+#\d"),
    re.compile(r"\bconsolidat", re.IGNORECASE),
    re.compile(r"\bthis MR\b", re.IGNORECASE),
    re.compile(r"\bsee MR\b", re.IGNORECASE),
    re.compile(r"\bsuperseded by\b", re.IGNORECASE),
    # Ticket / workstream-number references. A JIRA-style tracker key —
    # 2+ uppercase letters, a hyphen, 2+ digits — is the generic shape of
    # a ticket id left in a comment. Well-known standard/crypto prefixes
    # (SHA-256, RFC-3339, ISO-8601, …) share the shape but are not
    # bookkeeping; they are excluded in ``_matches_self_ref``.
    re.compile(r"\b[A-Z]{2,}-\d{2,}\b"),
    re.compile(r"#85\d\d\b"),
    re.compile(r"\bbugs#"),
    re.compile(r"\bW(?:0[1-9]|1\d|2\d)\b"),
    re.compile(r"\bsub-ticket\b", re.IGNORECASE),
    # Process narration.
    re.compile(r"\bper the spec\b", re.IGNORECASE),
    re.compile(r"\bper review\b", re.IGNORECASE),
    re.compile(r"\bas requested\b", re.IGNORECASE),
    re.compile(r"\bper pentest\b", re.IGNORECASE),
    re.compile(r"\bpentest\b", re.IGNORECASE),
)

# File suffixes whose languages use ``#`` line comments.
_HASH_COMMENT_SUFFIXES = (".py", ".sh", ".bash", ".rb", ".yml", ".yaml", ".toml")
# File suffixes whose languages use ``//`` line comments and ``/* */`` blocks.
_SLASH_COMMENT_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".c", ".cpp", ".cs", ".scss", ".css")

# Docs / markdown files legitimately cite MRs / tickets — fully exempt.
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
_DOC_PATH_PREFIXES = ("docs/",)
_DOC_BASENAME_PREFIXES = ("CHANGELOG",)

_FILE_HEADER_RE = re.compile(r"^\+\+\+ (?:b/)?(.+?)(?:\t.*)?$")


def _is_doc_file(path: str) -> bool:
    """True when ``path`` is markdown/docs and therefore exempt."""
    lowered = path.lower()
    if lowered.endswith(_DOC_SUFFIXES):
        return True
    if any(lowered.startswith(prefix) or f"/{prefix}" in lowered for prefix in _DOC_PATH_PREFIXES):
        return True
    basename = path.rsplit("/", 1)[-1]
    return any(basename.startswith(prefix) for prefix in _DOC_BASENAME_PREFIXES)


def _comment_text(path: str, code: str) -> str | None:
    """Return the comment portion of a code line, or ``None`` if no comment.

    Best-effort, language-aware on the FILE SUFFIX: ``#`` for hash-comment
    languages, ``//`` and ``/* */`` for slash-comment languages. A naive
    first-marker split is intentional — it can over-include a marker that
    sits inside a string literal, but the patterns it feeds only match
    bookkeeping refs, so a string like ``"https://x/-/merge_requests/7511"``
    (a bare-number URL, not a bang-MR ref, with no ``# comment``) does not
    trip. The goal is to AVOID scanning code identifiers/data, not to write
    a full parser.
    """
    lowered = path.lower()
    if lowered.endswith(_HASH_COMMENT_SUFFIXES):
        marker = code.find("#")
        return code[marker:] if marker != -1 else None
    if lowered.endswith(_SLASH_COMMENT_SUFFIXES):
        line_marker = code.find("//")
        block_open = code.find("/*")
        markers = [m for m in (line_marker, block_open) if m != -1]
        return code[min(markers) :] if markers else None
    return None


# Well-known standard / crypto identifiers that share the JIRA-key shape
# (``<UPPER>-<digits>``) but are not ticket bookkeeping.
_TRACKER_KEY_FALSE_POSITIVES = frozenset(
    {"SHA", "UTF", "ISO", "RFC", "AES", "RSA", "CVE", "UTC", "SOC", "FIPS", "PEP", "GMT", "CRC", "MD"}
)


def _matches_self_ref(comment: str) -> str | None:
    """Return the matched bookkeeping substring, or ``None``.

    A tracker-key-shaped match (``<UPPER>-<digits>``) whose prefix is a
    well-known standard/crypto identifier (``SHA-256``, ``RFC-3339``) is
    not bookkeeping and is skipped — other patterns on the same comment
    are still considered.
    """
    for pattern in _SELF_REF_PATTERNS:
        for match in pattern.finditer(comment):
            token = match.group()
            prefix = token.split("-", 1)[0]
            if "-" in token and prefix in _TRACKER_KEY_FALSE_POSITIVES:
                continue
            return token
    return None


def scan_diff(text: str) -> list[tuple[int, str, str]]:
    """Scan a unified diff for self-referential bookkeeping in code comments.

    Returns a list of ``(line_number, category, match)`` findings, where
    ``line_number`` is the 1-based position within ``text`` (so it lines up
    with the per-line findings ``privacy_scan.py`` emits). Only **added**
    lines (``+`` but not the ``+++`` file header) in **non-doc source
    files** are scanned, and only the **comment** portion of each.
    """
    findings: list[tuple[int, str, str]] = []
    current_path: str | None = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        header = _FILE_HEADER_RE.match(raw)
        if header is not None:
            current_path = header.group(1)
            continue
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        if current_path is None or _is_doc_file(current_path):
            continue
        added = raw[1:]
        if _ALLOW_MARKER in added:
            continue
        comment = _comment_text(current_path, added)
        if comment is None:
            continue
        match = _matches_self_ref(comment)
        if match is not None:
            findings.append((lineno, CATEGORY, match))
    return findings
