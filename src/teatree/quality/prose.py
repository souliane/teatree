"""Extract the prose from a source file: what a human wrote ABOUT the code.

Comments and docstrings for Python, everything outside a fenced block for
markdown. A phrase inside a string literal is deliberately not prose -- that is
a runtime value (a user-facing message, a PR-body template, a fixture) rather
than the module describing itself.

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


@dataclasses.dataclass(frozen=True)
class ProseLine:
    lineno: int
    text: str


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


def python_prose(source: str) -> list[ProseLine]:
    """Comment and docstring lines of a Python source, in file order."""
    lines: list[ProseLine] = []
    try:
        lines.extend(
            ProseLine(lineno=token.start[0], text=token.string)
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT
        )
        lines.extend(_docstring_prose(ast.parse(source)))
    except (SyntaxError, tokenize.TokenError, ValueError):
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
    return [dataclasses.replace(line, text=_LINK_TARGET_RE.sub("]", line.text)) for line in lines]
