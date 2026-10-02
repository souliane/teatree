"""Extract the prose from a source file: what a human wrote ABOUT the code.

Comments and docstrings for Python, everything outside a fenced block for
markdown. A phrase inside a string literal is deliberately not prose -- that is
a runtime value (a user-facing message, a PR-body template, a fixture) rather
than the module describing itself.

``file_comments`` is the narrower cut: only the lines a file addresses to its
READER, never a docstring. A docstring is the object documenting what it IS; a
comment is a note left for whoever reads the source next, which is the form an
author-marked deferral takes. In YAML and TOML that is a ``#`` outside a scalar,
with a block scalar's body skipped whole because it is verbatim carried text.
Markdown has no comment syntax, so its body is the note -- minus fenced and
indented code, the one part of a markdown file that is quoted rather than said.
JSON and JSONL have no comment syntax either, so they carry no reader-addressed
line at all.

Markdown link targets are dropped. A URL is punctuation-dense noise sitting
between a visible label and the rest of the sentence, and it breaks any pattern
scoped to a sentence on the dots in a hostname.

Split from ``incompleteness_markers``, which asks a separate question of these
lines: which of them declare the code unfinished.
"""

import ast
import dataclasses
import io
import re
import tokenize
from collections.abc import Iterator
from pathlib import Path

_LINK_TARGET_RE = re.compile(r"\]\([^)\s]*\)")

_DOCSTRING_HOLDERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

#: Configuration formats whose comment syntax is a ``#`` outside a quoted scalar.
HASH_COMMENT_SUFFIXES: tuple[str, ...] = (".yaml", ".yml", ".toml")

#: Every suffix ``file_comments`` can read a reader-addressed line from.
COMMENT_SUFFIXES: tuple[str, ...] = (".py", ".md", *HASH_COMMENT_SUFFIXES)

#: What may precede a quote for it to OPEN a scalar rather than sit inside one.
#: Without this an apostrophe in ``don't`` opens a region that never closes, and
#: the comment after it is swallowed.
_SCALAR_OPENERS = frozenset(" \t:,[{-=")

#: A block-scalar header (``|``, ``>-``, ``|2``): everything indented under it is
#: literal text, which is where a scenario quotes the marker it grades against.
_BLOCK_SCALAR_RE = re.compile(r"[|>][+-]?\d*\s*$")

#: An indented markdown code block. Markdown has no comment syntax, so its body
#: is the author's note -- except where the body is quoted code.
_MARKDOWN_INDENT = 4
_MARKDOWN_FENCES = ("```", "~~~")


@dataclasses.dataclass(frozen=True)
class ProseLine:
    lineno: int
    text: str


def _without_link_targets(lines: list[ProseLine]) -> list[ProseLine]:
    return [dataclasses.replace(line, text=_LINK_TARGET_RE.sub("]", line.text)) for line in lines]


def _docstring_prose(tree: ast.AST) -> Iterator[ProseLine]:
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_HOLDERS):
            continue
        body = node.body
        if not body or not isinstance(body[0], ast.Expr):
            continue
        literal = body[0].value
        if not isinstance(literal, ast.Constant) or not isinstance(literal.value, str):
            continue
        for offset, line in enumerate(literal.value.splitlines()):
            yield ProseLine(lineno=literal.lineno + offset, text=line)


def python_comments(source: str) -> list[ProseLine]:
    """The ``#`` comment lines of a Python source, in file order."""
    try:
        return [
            ProseLine(lineno=token.start[0], text=token.string)
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT
        ]
    except (SyntaxError, tokenize.TokenError, ValueError):
        return []


def comment_column(text: str) -> int | None:
    r"""Where a comment opens on a YAML/TOML *text*, or ``None`` if it carries none.

    Three things separate a comment sigil from a ``#`` that is part of a value,
    and getting any of them wrong costs a gate in one direction or the other: the
    sigil must follow whitespace or open the line, a quote only starts a scalar at
    the head of a token (so the apostrophe in ``don't`` does not), and both of
    YAML's escapes -- ``\\"`` inside double quotes, ``''`` inside single -- keep
    the scalar open.
    """
    quote: str | None = None
    column = 0
    while column < len(text):
        char = text[column]
        if quote is not None:
            if char == "\\" and quote == '"':
                column += 2
                continue
            if char == quote:
                quote = None if text[column + 1 : column + 2] != quote else quote
                column += 1 if quote is None else 2
                continue
        elif char in "'\"" and (column == 0 or text[column - 1] in _SCALAR_OPENERS):
            quote = char
        elif char == "#" and (column == 0 or text[column - 1] in " \t"):
            return column
        column += 1
    return None


def hash_comments(source: str) -> list[ProseLine]:
    """The ``#`` comment of each line of a YAML/TOML document, from the sigil onwards.

    A block scalar's body is skipped whole. It is verbatim text the document
    carries, which is exactly where an eval scenario quotes the marker vocabulary
    it grades an agent against.
    """
    lines: list[ProseLine] = []
    block_indent: int | None = None
    for lineno, text in enumerate(source.splitlines(), start=1):
        indent = len(text) - len(text.lstrip())
        if block_indent is not None:
            if not text.strip() or indent > block_indent:
                continue
            block_indent = None
        column = comment_column(text)
        if column is not None:
            lines.append(ProseLine(lineno=lineno, text=text[column:]))
        if _BLOCK_SCALAR_RE.search((text if column is None else text[:column]).rstrip()):
            block_indent = indent
    return lines


def markdown_notes(source: str) -> list[ProseLine]:
    """Markdown lines the author addressed to the reader: prose, never quoted code.

    Both fence styles and the indented-block form are excluded. Markdown has no
    comment syntax, so a code sample is the only thing in the document that is
    not the author speaking.
    """
    lines: list[ProseLine] = []
    fence: str | None = None
    for lineno, text in enumerate(source.splitlines(), start=1):
        stripped = text.lstrip()
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            continue
        opening = next((mark for mark in _MARKDOWN_FENCES if stripped.startswith(mark)), None)
        if opening is not None:
            fence = opening
            continue
        if text[:_MARKDOWN_INDENT].strip() or not text.startswith(" " * _MARKDOWN_INDENT):
            lines.append(ProseLine(lineno=lineno, text=text))
    return lines


def python_prose(source: str) -> list[ProseLine]:
    """Comment and docstring lines of a Python source, in file order."""
    lines = python_comments(source)
    try:
        lines = [*lines, *_docstring_prose(ast.parse(source))]
    except (SyntaxError, ValueError):
        return []
    return sorted(lines, key=lambda line: line.lineno)


def markdown_prose(source: str) -> list[ProseLine]:
    """Every markdown line outside a fenced code block."""
    lines: list[ProseLine] = []
    fenced = False
    for lineno, text in enumerate(source.splitlines(), start=1):
        if text.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(ProseLine(lineno=lineno, text=text))
    return lines


def file_prose(path: Path) -> list[ProseLine]:
    """The prose of *path*, dispatched on suffix and normalised for link targets."""
    source = path.read_text(encoding="utf-8", errors="replace")
    lines = python_prose(source) if path.suffix == ".py" else markdown_prose(source)
    return _without_link_targets(lines)


def file_comments(path: Path) -> list[ProseLine]:
    """The reader-addressed lines of *path*: its comments, or a markdown body."""
    source = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".py":
        return _without_link_targets(python_comments(source))
    if path.suffix in HASH_COMMENT_SUFFIXES:
        return _without_link_targets(hash_comments(source))
    if path.suffix == ".md":
        return _without_link_targets(markdown_notes(source))
    return []
