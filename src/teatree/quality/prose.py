"""Extract the prose from a source file: what a human wrote ABOUT the code.

Comments and docstrings for Python, everything outside a fenced block for
markdown. A phrase inside a string literal is deliberately not prose -- that is
a runtime value (a user-facing message, a PR-body template, a fixture) rather
than the module describing itself.

``file_comments`` is the narrower cut: only the lines a file addresses to its
READER, never a docstring. A docstring is the object documenting what it IS; a
comment is a note left for whoever reads the source next, which is the form an
author-marked deferral takes. Markdown has no comment syntax, so its whole body
outside a fence is that note; JSON and JSONL have none either and carry no
reader-addressed line at all.

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


def hash_comments(source: str) -> list[ProseLine]:
    """The ``#`` comment of each line of a YAML/TOML document, from the sigil onwards.

    A ``#`` inside a quoted scalar is a character of that value, so the scan
    tracks quoting rather than splitting on the first sigil it sees.
    """
    lines: list[ProseLine] = []
    for lineno, text in enumerate(source.splitlines(), start=1):
        quote: str | None = None
        for column, char in enumerate(text):
            if quote is not None:
                quote = None if char == quote else quote
            elif char in "'\"":
                quote = char
            elif char == "#":
                lines.append(ProseLine(lineno=lineno, text=text[column:]))
                break
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
        return _without_link_targets(markdown_prose(source))
    return []
