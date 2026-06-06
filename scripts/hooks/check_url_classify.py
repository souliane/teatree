r"""Ban PR/MR-URL classification regexes outside teatree.url_classify.

Forge-URL parsing (is this a GitHub PR or a GitLab MR, what repo/number does it
name) belongs in :mod:`teatree.url_classify` (built on
:mod:`teatree.utils.url_slug`). The parser had drifted into a dozen near-identical
``re.compile`` patterns across the scanners; this hook keeps the path grammar in
one home.

Flagged: an ``re.compile(...)`` whose pattern string carries the PR/MR-URL
classification shape — the ``merge_requests`` / ``pull(s)`` path-segment
alternation, or the ``/-/merge_requests/(?P<...>\\d+)`` GitLab-MR shape. The
forge backends (``teatree.backends.*``) legitimately own their API endpoint
grammar and are exempt, along with the two URL modules themselves.

AST-based (only ``re.compile`` literals match, so f-string URL *constructors*,
docstrings, and fixture URLs never false-positive). Diff-scoped via filename
args in pre-commit; ``--all`` walks the whole tree (the CI / conformance path).
"""

import argparse
import ast
import pathlib
import re
import sys

TARGET_PREFIXES: tuple[str, ...] = ("src/teatree/",)

_ALLOWED_MODULES: frozenset[str] = frozenset(
    {
        "teatree.url_classify",
        "teatree.utils.url_slug",
    }
)
_ALLOWED_PACKAGE_PREFIXES: tuple[str, ...] = ("teatree.backends.",)

# The web-URL classification shape: a slash-delimited path-segment alternation
# over the three PR/MR segments. Deliberately narrower than the forge *API*
# endpoint matchers in eval/transcript_conformance.py (``merge_requests|pulls``
# with no leading slash and no ``pull`` singular), which are a distinct concern.
_PR_ALTERNATION_RE = re.compile(r"/\(\?:merge_requests\|pull\|pulls\)")
_GITLAB_MR_SHAPE_RE = re.compile(r"/-/merge_requests/\(\?P<\w+>\\d\+\)")


def module_path_for(rel: str) -> str:
    parts = pathlib.PurePosixPath(rel.removeprefix("src/")).with_suffix("").parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _is_allowed(module_path: str) -> bool:
    return module_path in _ALLOWED_MODULES or module_path.startswith(_ALLOWED_PACKAGE_PREFIXES)


def _is_re_compile(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "compile"
        and isinstance(func.value, ast.Name)
        and func.value.id == "re"
    )


def _pattern_literal(node: ast.Call) -> str | None:
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _is_pr_url_pattern(pattern: str) -> bool:
    return bool(_PR_ALTERNATION_RE.search(pattern)) or bool(_GITLAB_MR_SHAPE_RE.search(pattern))


def _scan_file(path: pathlib.Path) -> list[int]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_re_compile(node):
            continue
        pattern = _pattern_literal(node)
        if pattern is not None and _is_pr_url_pattern(pattern):
            hits.append(node.lineno)
    return hits


def _normalize(path_arg: str) -> str:
    p = pathlib.Path(path_arg)
    try:
        return str(p.resolve().relative_to(pathlib.Path.cwd())).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def _in_scope(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in TARGET_PREFIXES)


def _iter_tree() -> list[str]:
    root = pathlib.Path("src/teatree")
    return [str(p).replace("\\", "/") for p in sorted(root.rglob("*.py"))]


def check(paths: list[str]) -> int:
    rc = 0
    for raw in paths:
        rel = _normalize(raw)
        if not _in_scope(rel) or _is_allowed(module_path_for(rel)):
            continue
        for lineno in _scan_file(pathlib.Path(raw)):
            sys.stderr.write(
                f"{rel}:{lineno}: PR/MR-URL classification regex is not allowed here — "
                "route forge-URL parsing through teatree.url_classify "
                "(forge_of / pr_ref / repo_and_iid / find_pr_urls).\n",
            )
            rc = 1
    return rc


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Ban PR/MR-URL classification regexes outside url_classify.")
    parser.add_argument("--all", action="store_true", help="walk the whole src/teatree tree")
    parser.add_argument("paths", nargs="*", help="files to check (pre-commit passes the staged diff)")
    args = parser.parse_args(argv)
    paths = _iter_tree() if args.all else args.paths
    return check(paths)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
