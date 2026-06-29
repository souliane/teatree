"""``code_comment_density`` pass for the comments-as-code rule.

Commit-side half of the comments-as-code rule (names + types are the
documentation; a long comment is a code smell — refactor instead of
explain): a diff pass that catches WHAT-narration the content-aware
``code_comment_self_reference`` detector (tracker tokens) misses.

Advisory — its consumers (the ``check_comment_density.py`` pre-push hook,
the ``comment-density-gate`` CI job, the ``t3 tool comment-density``
command) print findings and exit 0. Not one of ``privacy_scan.py``'s
blocking diff detectors: a comment-dense diff is a code-quality nudge, not
a privacy leak.

A file's ADDED lines are flagged when ANY of three signals hold. The
content-aware **restatement** signal flags a comment whose meaningful words
merely restate the following code line's identifier tokens (a narration verb
plus tokens already in the code, no non-obvious why), OR a docstring opening
line that merely echoes the signature name — a single such line is enough.
This is the verbose-docstring + restating-inline abuse the line-count pass
missed. The **consecutive-run** signal flags a run of comment-only lines past
the warn threshold. The **ratio** signal flags added comment-only to added
code lines past a conservative threshold (with floors so a tiny diff or a
lone explanatory comment never trips).

Multi-line comments and docstrings stay ALLOWED when justified — a genuine
non-obvious why (words the code does not contain, not bare narration) does
not restate and is never flagged. Comment syntax is decided on the FILE
SUFFIX (Python ``#``, JS/TS ``//`` and ``/* */``). Exempt: tooling pragmas
(``# type:``/``# noqa``/``# pragma`` / ``pyright:``/``mypy:``/``ruff:`` /
``// eslint-disable`` / ``@ts-ignore`` / coverage ``istanbul``/``c8``); the
``security:``-prefixed threat-model allowlist; a file-LEADING comment block
(license / shebang / banner preamble) and a license-marker header further
down; markdown/docs, declarative config, and ``tests/``.
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

_RESTATE_OVERLAP_THRESHOLD = 0.6
_MIN_RESTATE_WORDS = 2
_ONE_LINE_DOCSTRING_MARKERS = 2
_WORD_RE = re.compile(r"[a-z]+")
_DEF_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# WHAT-narration verbs that merely announce the code on the next line; a
# comment built only from these plus tokens already in that code carries no
# non-obvious why.
_NARRATION_VERBS = frozenset(
    {
        "add",
        "adds",
        "append",
        "apply",
        "applies",
        "assign",
        "build",
        "call",
        "calls",
        "check",
        "compute",
        "convert",
        "create",
        "creates",
        "delete",
        "deletes",
        "divide",
        "fetch",
        "filter",
        "format",
        "get",
        "increment",
        "initialise",
        "initialize",
        "iterate",
        "load",
        "loop",
        "make",
        "multiply",
        "remove",
        "removes",
        "return",
        "returns",
        "round",
        "save",
        "seed",
        "set",
        "sets",
        "store",
        "stores",
        "sum",
        "update",
        "updates",
        "write",
    }
)

# Filler that carries no signal — dropped from a comment's content-word set so
# the overlap ratio reflects the meaningful words only.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "by",
        "every",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "of",
        "on",
        "one",
        "or",
        "out",
        "row",
        "rows",
        "so",
        "the",
        "then",
        "this",
        "to",
        "value",
        "values",
        "with",
    }
)


def _content_words(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS]


def _code_tokens(code: str) -> set[str]:
    return set(_WORD_RE.findall(code.lower()))


def _signature_name(code: str) -> str | None:
    match = _DEF_RE.match(code)
    return match.group(1) if match else None


def _name_words(name: str) -> set[str]:
    spaced = _CAMEL_BOUNDARY_RE.sub(" ", name).replace("_", " ")
    return {w for w in _WORD_RE.findall(spaced.lower()) if w not in _STOPWORDS}


def _restates(words: list[str], code_tokens: set[str]) -> bool:
    """True when a comment's content-words merely restate the code's tokens.

    Restatement = a strong majority of the meaningful words are either already
    an identifier token in the code line or a bare narration verb announcing it,
    with at least one word landing in the code. Several words the code does not
    contain and that are not narration (a non-obvious WHY) break restatement.
    """
    if len(words) < _MIN_RESTATE_WORDS:
        return False
    in_code = sum(1 for w in words if w in code_tokens)
    accounted = sum(1 for w in words if w in code_tokens or w in _NARRATION_VERBS)
    return in_code >= 1 and accounted / len(words) >= _RESTATE_OVERLAP_THRESHOLD


def _stem_matches(word: str, name_words: set[str]) -> bool:
    return any(word == n or word.startswith(n) or n.startswith(word) for n in name_words)


def _echoes_signature(words: list[str], name_words: set[str]) -> bool:
    if len(words) < _MIN_RESTATE_WORDS:
        return False
    in_name = sum(1 for w in words if _stem_matches(w, name_words))
    accounted = sum(1 for w in words if _stem_matches(w, name_words) or w in _NARRATION_VERBS)
    return in_name >= 1 and accounted == len(words)


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
        self.restatements = 0
        self.in_docstring = False
        self.code_seen = False
        self._run = 0
        self._target_line = 0
        self._last_signature: str | None = None
        self._pending_prose: list[list[str]] = []

    def set_hunk_start(self, new_start: int) -> None:
        self._target_line = new_start - 1

    def feed_line(self, raw: str) -> None:
        comment_re = self.comment_re
        if comment_re is None:
            return
        if raw.startswith(" "):
            self._feed_context_line(raw[1:])
            return
        if not raw.startswith("+") or raw.startswith("+++"):
            return
        self._feed_added_line(raw[1:], comment_re)

    def _feed_context_line(self, code: str) -> None:
        self._target_line += 1
        if code.strip():
            self.code_seen = True
            self._resolve_against_code(code)

    def _feed_added_line(self, code: str, comment_re: re.Pattern[str]) -> None:
        self._target_line += 1
        if _ALLOW_MARKER in code:
            return
        was_in_docstring = self.in_docstring
        if self._consume_docstring(code) or was_in_docstring:
            self._feed_docstring_line(code, opening=not was_in_docstring)
            return
        if _is_security_rationale(code):
            return
        is_comment = bool(comment_re.match(code))
        if is_comment and _is_pragma(code):
            return
        self._classify(code, is_comment=is_comment)

    def _consume_docstring(self, code: str) -> bool:
        """Update docstring state; return whether this line touches a docstring.

        A line with an odd count of triple-quote markers toggles the multi-line
        docstring state. A line that opens AND closes on itself (even count, but
        the stripped line starts with a triple quote) is a one-line docstring —
        the state does not change, but the line is still a docstring line.
        """
        markers = len(_TRIPLE_QUOTE_RE.findall(code))
        if markers % 2 == 1:
            self.in_docstring = not self.in_docstring
            return True
        return markers >= _ONE_LINE_DOCSTRING_MARKERS and _TRIPLE_QUOTE_RE.match(code.lstrip()) is not None

    def _feed_docstring_line(self, code: str, *, opening: bool) -> None:
        words = _content_words(_TRIPLE_QUOTE_RE.sub(" ", code))
        if opening and self._last_signature is not None and _echoes_signature(words, _name_words(self._last_signature)):
            self.restatements += 1
            return
        if words:
            self._pending_prose.append(words)

    def _classify(self, code: str, *, is_comment: bool) -> None:
        if is_comment:
            if self._is_header_comment(code):
                return
            self.comment_lines += 1
            self._run += 1
            self.max_consecutive = max(self.max_consecutive, self._run)
            words = _content_words(code.lstrip("#/ *"))
            if words:
                self._pending_prose.append(words)
        else:
            self._run = 0
            if code.strip():
                self.code_lines += 1
                self.code_seen = True
                signature = _signature_name(code)
                if signature is not None:
                    self._last_signature = signature
                self._resolve_against_code(code)

    def _resolve_against_code(self, code: str) -> None:
        if not self._pending_prose:
            return
        tokens = _code_tokens(code)
        self.restatements += sum(1 for words in self._pending_prose if _restates(words, tokens))
        self._pending_prose = []

    def _is_header_comment(self, code: str) -> bool:
        if not self.code_seen and self._target_line <= _LEADING_HEADER_MAX_LINE:
            return True
        return _is_license_marker(code)

    @property
    def is_flagged(self) -> bool:
        if self.restatements > 0:
            return True
        if self.max_consecutive > _CONSECUTIVE_COMMENT_WARN_THRESHOLD:
            return True
        if self.comment_lines < _MIN_ADDED_COMMENT_LINES or self.code_lines < _MIN_ADDED_CODE_LINES:
            return False
        return self.comment_lines > self.code_lines * _RATIO_THRESHOLD

    @property
    def reason(self) -> str:
        if self.restatements > 0:
            return f"{self.restatements} comment/docstring line(s) restate the code"
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
    restatements: int
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
            restatements=scan.restatements,
            reason=scan.reason,
        )
        for _, path, scan in _iter_file_scans(text)
        if scan.is_flagged
    ]
