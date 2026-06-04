"""``code_comment_density`` density pass for the near-zero-comments rule.

This module is the commit-side half of the near-zero-comments rule
(names + types are the documentation): a **content-blind** density pass
that catches the plain WHAT-narration the content-aware
``code_comment_self_reference`` detector misses (a run of comments that
merely restate what the code already says, with no tracker token to match).

The check is **advisory** — its consumers (the ``check_comment_density.py``
pre-push hook, the ``comment-density-gate`` CI job, and the
``t3 tool comment-density`` command) print the findings as a warning and
exit 0. There is no good content-blind heuristic for "overly long prose"
that does not also flag legitimate long comments, so the signal is
surfaced without blocking the commit, push, or pipeline. It is deliberately
NOT one of ``privacy_scan.py``'s blocking diff detectors — a comment-dense
diff is a code-quality nudge, not a privacy leak.

A file's ADDED lines are flagged when EITHER the ratio of added
comment-only lines to added code lines exceeds a conservative threshold
(with a floor on added code lines so a tiny diff cannot trip it), OR there
is a block of consecutive comment-only lines past the warn threshold.

Comment-only is decided language-aware on the FILE SUFFIX (Python ``#``,
JS/TS ``//`` and ``/* */`` line/block markers). Exempt from the comment
count: docstring bodies (lines inside a triple-quoted block); **tooling
pragmas** that are machine directives, not prose (``# type:``, ``# noqa``,
``# pragma``, ``pyright:``/``mypy:``/``ruff:``, ``// eslint-disable``,
``// @ts-ignore``/``@ts-expect-error``, coverage ``istanbul``/``c8`` ignores);
and a small **security-rationale** allowlist (a comment whose text begins
with the agreed ``security:`` marker — a deliberate threat-model note, not
WHAT-narration). Also exempt is a file-LEADING comment block — a run that
begins at the top of a file's added lines (within a shebang / encoding /
blank offset) before any code, which is a license / copyright / banner
preamble rather than WHAT-narration — plus a narrow license-marker
fallback for a header further down (a comment carrying ``SPDX-License-Identifier``,
``Copyright``, ``Licensed under`` or ``All rights reserved``). Fully exempt
files: markdown/docs (``*.md``, ``docs/``, ``CHANGELOG*``), declarative
config (``*.yml``/``*.yaml``/``*.toml``/``*.cfg``/``*.ini`` — a CI job's
"why this exists" block has no names+types alternative), and ``tests/``
(test bodies legitimately narrate intent).

The thresholds are deliberately conservative: the ratio rule applies only
once a meaningful number of comment lines is present (the comment-line
floor) and there are enough code lines to compare against (the code-line
floor), so a single explanatory comment never trips it however small the
diff. A run of consecutive comment lines past the warn threshold is the
strong WHAT-narration signal and warns on its own.
"""

import re
from dataclasses import dataclass

_ALLOW_MARKER = "privacy-scan:allow"
_SECURITY_RATIONALE_MARKER = "security:"

CATEGORY = "code_comment_density"

_PRAGMA_TOKENS = (
    "type:",
    "noqa",
    r"pragma\b",
    "pyright:",
    "mypy:",
    "ruff:",
    "eslint-disable",
    "eslint-enable",
    "prettier-ignore",
    r"@ts-(?:ignore|expect-error|nocheck)",
    r"istanbul\s+ignore",
    r"c8\s+ignore",
    r"v8\s+ignore",
    "biome-ignore",
)
_PRAGMA_RE = re.compile(
    r"(?:\#|//|/\*|\*)\s*(?:" + "|".join(_PRAGMA_TOKENS) + r")",
    re.IGNORECASE,
)

_RATIO_THRESHOLD = 0.15
_MIN_ADDED_CODE_LINES = 3
_MIN_ADDED_COMMENT_LINES = 3
_CONSECUTIVE_COMMENT_WARN_THRESHOLD = 2

_HASH_COMMENT_SUFFIXES = (".py", ".sh", ".bash", ".rb")
_SLASH_COMMENT_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".c", ".cpp", ".cs", ".scss", ".css")

_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
_CONFIG_SUFFIXES = (".yml", ".yaml", ".toml", ".cfg", ".ini")
_DOC_PATH_PREFIXES = ("docs/",)
_DOC_BASENAME_PREFIXES = ("CHANGELOG",)
_TEST_PATH_PREFIXES = ("tests/", "test/")

_FILE_HEADER_RE = re.compile(r"^\+\+\+ (?:b/)?(.+?)(?:\t.*)?$")
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_HASH_COMMENT_RE = re.compile(r"^\s*#")
_SLASH_COMMENT_RE = re.compile(r"^\s*(?://|/\*|\*)")
_TRIPLE_QUOTE_RE = re.compile(r'"""|\'\'\'')

_LEADING_HEADER_MAX_LINE = 5
_LICENSE_MARKER_RE = re.compile(
    r"spdx-license-identifier|copyright|licensed under|all rights reserved",
    re.IGNORECASE,
)


def _is_exempt_file(path: str) -> bool:
    lowered = path.lower()
    if lowered.endswith((*_DOC_SUFFIXES, *_CONFIG_SUFFIXES)):
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


def _is_license_marker(code: str) -> bool:
    return _LICENSE_MARKER_RE.search(code) is not None


def _is_pragma(code: str) -> bool:
    return _PRAGMA_RE.match(code.lstrip()) is not None


class _FileScan:
    def __init__(self, comment_re: re.Pattern[str] | None) -> None:
        self.comment_re = comment_re
        self.comment_lines = 0
        self.code_lines = 0
        self.max_consecutive = 0
        self.in_docstring = False
        self.code_seen = False
        self._run = 0
        self._target_line = 0

    def set_hunk_start(self, new_start: int) -> None:
        self._target_line = new_start - 1

    def feed_line(self, raw: str) -> None:
        if self.comment_re is None:
            return
        if raw.startswith(" "):
            self._target_line += 1
            if raw[1:].strip():
                self.code_seen = True
            return
        if not raw.startswith("+") or raw.startswith("+++"):
            return
        self._target_line += 1
        code = raw[1:]
        if _ALLOW_MARKER in code or self._consume_docstring(code) or _is_security_rationale(code):
            return
        is_comment = bool(self.comment_re.match(code))
        if is_comment and _is_pragma(code):
            return
        self._classify(code, is_comment=is_comment)

    def _consume_docstring(self, code: str) -> bool:
        was_in_docstring = self.in_docstring
        if len(_TRIPLE_QUOTE_RE.findall(code)) % 2 == 1:
            self.in_docstring = not self.in_docstring
        return was_in_docstring or self.in_docstring

    def _classify(self, code: str, *, is_comment: bool) -> None:
        if is_comment:
            if self._is_header_comment(code):
                return
            self.comment_lines += 1
            self._run += 1
            self.max_consecutive = max(self.max_consecutive, self._run)
        else:
            self._run = 0
            if code.strip():
                self.code_lines += 1
                self.code_seen = True

    def _is_header_comment(self, code: str) -> bool:
        if not self.code_seen and self._target_line <= _LEADING_HEADER_MAX_LINE:
            return True
        return _is_license_marker(code)

    @property
    def is_flagged(self) -> bool:
        if self.max_consecutive > _CONSECUTIVE_COMMENT_WARN_THRESHOLD:
            return True
        if self.comment_lines < _MIN_ADDED_COMMENT_LINES or self.code_lines < _MIN_ADDED_CODE_LINES:
            return False
        return self.comment_lines > self.code_lines * _RATIO_THRESHOLD

    @property
    def reason(self) -> str:
        if self.max_consecutive > _CONSECUTIVE_COMMENT_WARN_THRESHOLD:
            return f"{self.max_consecutive} consecutive comment-only lines"
        return (
            f"{self.comment_lines} added comment lines vs {self.code_lines} added code lines "
            f"(ratio {self.ratio:.2f} > {_RATIO_THRESHOLD:.2f})"
        )

    @property
    def ratio(self) -> float:
        return self.comment_lines / self.code_lines if self.code_lines else 0.0


@dataclass(frozen=True)
class CommentDensityFinding:
    """One comment-dense file flagged in a diff."""

    path: str
    comment_lines: int
    code_lines: int
    max_consecutive: int
    reason: str

    @property
    def ratio(self) -> float:
        return self.comment_lines / self.code_lines if self.code_lines else 0.0

    def render(self) -> str:
        return f"{self.path}: comment-dense added lines — {self.reason}"


def _iter_file_scans(text: str) -> "list[tuple[int, str, _FileScan]]":
    """Drive the diff through one ``_FileScan`` per file header.

    Returns ``(header_lineno, path, scan)`` for every file in the diff,
    flagged or not. ``header_lineno`` is the 1-based position of the
    ``+++`` line within ``text``. Drives :func:`report_diff`.
    """
    results: list[tuple[int, str, _FileScan]] = []
    current_path: str | None = None
    header_lineno = 0
    scan = _FileScan(None)

    def flush() -> None:
        if current_path is not None:
            results.append((header_lineno, current_path, scan))

    for lineno, raw in enumerate(text.splitlines(), 1):
        header = _FILE_HEADER_RE.match(raw)
        if header is not None:
            flush()
            current_path = header.group(1)
            comment_re = None if _is_exempt_file(current_path) else _comment_re(current_path)
            header_lineno = lineno
            scan = _FileScan(comment_re)
            continue
        hunk = _HUNK_HEADER_RE.match(raw)
        if hunk is not None:
            scan.set_hunk_start(int(hunk.group(1)))
            continue
        scan.feed_line(raw)
    flush()
    return results


def report_diff(text: str) -> list[CommentDensityFinding]:
    """Structured per-file findings for the advisory ``comment-density`` tool.

    Only added lines (``+`` but not ``+++``) in non-exempt source files are
    counted; docstring bodies, tooling pragmas, and security-rationale
    comments are excluded from the comment tally. Each flagged file carries
    its added comment/code counts, the longest consecutive-comment run, and
    a human-readable reason so the CLI / hook can print an actionable warning
    rather than a bare "comment-dense" verdict.
    """
    return [
        CommentDensityFinding(
            path=path,
            comment_lines=scan.comment_lines,
            code_lines=scan.code_lines,
            max_consecutive=scan.max_consecutive,
            reason=scan.reason,
        )
        for _, path, scan in _iter_file_scans(text)
        if scan.is_flagged
    ]
