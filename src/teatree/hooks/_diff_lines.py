r"""Shared unified-diff added-line parsing for the two privacy diff hooks.

Both privacy diff hooks walk a unified diff's ADDED lines scoped by file path
and language: the ``code_comment_self_reference`` leak scan
(:mod:`privacy_diff_comments`) and the advisory ``code_comment_density`` pass
(:mod:`privacy_diff_comment_density`). This module holds the parsing primitives
they share — the ``+++`` file-header regex, the added-line predicate, the
added-line generator, and the doc-exempt / slash-comment suffix tables — so the
two hooks keep one copy of the diff plumbing and diverge only in their detection
logic.

Not routed through :func:`teatree.quality.gate_relaxation.parse_diff`: that
parser groups per-file added/removed bodies but drops each added line's 1-based
position within the diff text (the leak scan needs it to line up with
``privacy_scan.py``'s per-line findings) and requires a ``b/`` header prefix,
whereas these hooks accept a bare ``+++ <path>`` header and strip trailing tab
metadata (``+++ b/<path>\t<timestamp>``). The density hook additionally consumes
context lines and hunk headers, which an added-only iterator cannot carry — so
it reuses the primitives below rather than the generator.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass

# A unified-diff ``+++ b/<path>`` new-file header. The ``b/`` prefix is optional
# and any trailing tab metadata (``\t<timestamp>``) is stripped, so ``path`` is
# the repo-relative file path.
FILE_HEADER_RE = re.compile(r"^\+\+\+ (?:b/)?(.+?)(?:\t.*)?$")

# File suffixes whose languages use ``//`` line comments and ``/* */`` blocks.
SLASH_COMMENT_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".c", ".cpp", ".cs", ".scss", ".css")

# Docs / markdown files legitimately cite MRs / tickets and carry prose — both
# hooks exempt them (density additionally exempts config + tests; see its
# ``_is_exempt_file``).
DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
DOC_PATH_PREFIXES = ("docs/",)
DOC_BASENAME_PREFIXES = ("CHANGELOG",)


def is_added_line(raw: str) -> bool:
    """True for a unified-diff added body line (``+…`` but not the ``+++`` header)."""
    return raw.startswith("+") and not raw.startswith("+++")


def is_doc_path(path: str) -> bool:
    """True when ``path`` is markdown/docs (suffix, a ``docs/`` segment, or ``CHANGELOG*``)."""
    lowered = path.lower()
    if lowered.endswith(DOC_SUFFIXES):
        return True
    if any(lowered.startswith(prefix) or f"/{prefix}" in lowered for prefix in DOC_PATH_PREFIXES):
        return True
    basename = path.rsplit("/", 1)[-1]
    return any(basename.startswith(prefix) for prefix in DOC_BASENAME_PREFIXES)


@dataclass(frozen=True)
class AddedLine:
    """One added line in a unified diff, with its enclosing-file context.

    ``lineno`` is the 1-based position within the whole diff text (so it lines
    up with ``privacy_scan.py``'s per-line findings), ``path`` is the enclosing
    file's repo-relative path, and ``body`` is the line with its leading ``+``
    stripped.
    """

    lineno: int
    path: str
    body: str


def iter_added_lines(text: str) -> Iterator[AddedLine]:
    """Yield each added line in a unified diff with its file context.

    Walks ``text`` line by line, tracking the current ``+++`` file header. An
    added body line that appears before any file header — a malformed diff — is
    skipped, since it has no path context.
    """
    current_path: str | None = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        header = FILE_HEADER_RE.match(raw)
        if header is not None:
            current_path = header.group(1)
            continue
        if not is_added_line(raw):
            continue
        if current_path is None:
            continue
        yield AddedLine(lineno=lineno, path=current_path, body=raw[1:])
