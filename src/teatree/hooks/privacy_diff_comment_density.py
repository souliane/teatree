"""``code_comment_density`` detector for the diff privacy-scanner.

The diff privacy-scanner (``scripts/privacy_scan.py`` →
``t3 tool privacy-scan``, wired into the pre-push gate
``scripts/hooks/refuse-public-push-with-leak.sh``) scans the pushed diff
for emails / keys / IPs / banned terms, plus the content-aware
``code_comment_self_reference`` detector for bookkeeping refs left in
comments. This module adds the commit-side half of the near-zero-comments
rule: a **content-blind** density pass that catches the plain
WHAT-narration the self-reference detector misses (a run of comments that
merely restate what the code already says, with no tracker token to match).

A file's ADDED lines are flagged when EITHER the ratio of added
comment-only lines to added code lines exceeds a conservative threshold
(with a floor on added code lines so a tiny diff cannot trip it), OR there
is a block of 3+ consecutive comment-only lines.

Comment-only is decided language-aware on the FILE SUFFIX (Python ``#``,
JS/TS ``//`` and ``/* */`` line/block markers). Exempt from the comment
count: docstring bodies (lines inside a triple-quoted block) and
a small **security-rationale** allowlist (a comment whose text begins with
the agreed ``security:`` marker — a deliberate threat-model note, not
WHAT-narration). Fully exempt files: markdown/docs (``*.md``, ``docs/``,
``CHANGELOG*``) and ``tests/`` (test bodies legitimately narrate intent).

The thresholds are deliberately conservative: the ratio rule applies only
once a meaningful number of comment lines is present (the comment-line
floor) and there are enough code lines to compare against (the code-line
floor), so a single explanatory comment never trips it however small the
diff. A run of 3+ consecutive comment lines is the strong WHAT-narration
signal and flags on its own.
"""

import re

_ALLOW_MARKER = "privacy-scan:allow"
_SECURITY_RATIONALE_MARKER = "security:"

CATEGORY = "code_comment_density"

_RATIO_THRESHOLD = 0.15
_MIN_ADDED_CODE_LINES = 3
_MIN_ADDED_COMMENT_LINES = 3
_MAX_CONSECUTIVE_COMMENT_LINES = 2

_HASH_COMMENT_SUFFIXES = (".py", ".sh", ".bash", ".rb", ".yml", ".yaml", ".toml")
_SLASH_COMMENT_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".c", ".cpp", ".cs", ".scss", ".css")

_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
_DOC_PATH_PREFIXES = ("docs/",)
_DOC_BASENAME_PREFIXES = ("CHANGELOG",)
_TEST_PATH_PREFIXES = ("tests/", "test/")

_FILE_HEADER_RE = re.compile(r"^\+\+\+ (?:b/)?(.+?)(?:\t.*)?$")
_HASH_COMMENT_RE = re.compile(r"^\s*#")
_SLASH_COMMENT_RE = re.compile(r"^\s*(?://|/\*|\*)")
_TRIPLE_QUOTE_RE = re.compile(r'"""|\'\'\'')


def _is_exempt_file(path: str) -> bool:
    lowered = path.lower()
    if lowered.endswith(_DOC_SUFFIXES):
        return True
    if any(lowered.startswith(p) or f"/{p}" in lowered for p in (*_DOC_PATH_PREFIXES, *_TEST_PATH_PREFIXES)):
        return True
    basename = path.rsplit("/", 1)[-1]
    return any(basename.startswith(p) for p in _DOC_BASENAME_PREFIXES)


def _comment_re(path: str) -> re.Pattern[str] | None:
    lowered = path.lower()
    if lowered.endswith(_HASH_COMMENT_SUFFIXES):
        return _HASH_COMMENT_RE
    if lowered.endswith(_SLASH_COMMENT_SUFFIXES):
        return _SLASH_COMMENT_RE
    return None


def _is_security_rationale(code: str) -> bool:
    stripped = code.lstrip()
    for marker in ("#", "//", "/*", "*"):
        if stripped.startswith(marker):
            return stripped[len(marker) :].lstrip().lower().startswith(_SECURITY_RATIONALE_MARKER)
    return False


class _FileScan:
    def __init__(self) -> None:
        self.comment_lines = 0
        self.code_lines = 0
        self.max_consecutive = 0
        self.in_docstring = False
        self._run = 0

    def feed(self, code: str, *, is_comment: bool) -> None:
        if is_comment:
            self.comment_lines += 1
            self._run += 1
            self.max_consecutive = max(self.max_consecutive, self._run)
        else:
            self._run = 0
            if code.strip():
                self.code_lines += 1

    @property
    def is_flagged(self) -> bool:
        if self.max_consecutive > _MAX_CONSECUTIVE_COMMENT_LINES:
            return True
        if self.comment_lines < _MIN_ADDED_COMMENT_LINES or self.code_lines < _MIN_ADDED_CODE_LINES:
            return False
        return self.comment_lines > self.code_lines * _RATIO_THRESHOLD


def _toggle_docstring(scan: _FileScan, code: str) -> None:
    if len(_TRIPLE_QUOTE_RE.findall(code)) % 2 == 1:
        scan.in_docstring = not scan.in_docstring


def scan_diff(text: str) -> list[tuple[int, str, str]]:
    """Scan a unified diff for comment-dense added code, one finding per file.

    Returns ``(line_number, category, match)`` findings where ``line_number``
    is the 1-based position of the file header within ``text`` (so it lines
    up with the per-line findings ``privacy_scan.py`` emits). Only added
    lines (``+`` but not ``+++``) in non-exempt source files are counted, and
    docstring bodies plus security-rationale comments are excluded from the
    comment tally.
    """
    findings: list[tuple[int, str, str]] = []
    current_path: str | None = None
    comment_re: re.Pattern[str] | None = None
    header_lineno = 0
    scan = _FileScan()

    def flush() -> None:
        if current_path is not None and scan.is_flagged:
            findings.append((header_lineno, CATEGORY, f"{current_path}: comment-dense added lines"))

    for lineno, raw in enumerate(text.splitlines(), 1):
        header = _FILE_HEADER_RE.match(raw)
        if header is not None:
            flush()
            current_path = header.group(1)
            comment_re = None if _is_exempt_file(current_path) else _comment_re(current_path)
            header_lineno = lineno
            scan = _FileScan()
            continue
        if comment_re is None or not raw.startswith("+") or raw.startswith("+++"):
            continue
        code = raw[1:]
        if _ALLOW_MARKER in code:
            continue
        was_in_docstring = scan.in_docstring
        _toggle_docstring(scan, code)
        if was_in_docstring or scan.in_docstring:
            continue
        if _is_security_rationale(code):
            continue
        scan.feed(code, is_comment=bool(comment_re.match(code)))
    flush()
    return findings
