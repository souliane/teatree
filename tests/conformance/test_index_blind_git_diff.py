"""No ``git diff`` in ``src/`` may decide anything from the working tree alone.

``git diff`` with no revision compares the working tree to the INDEX, so a
checkout whose entire delta is STAGED reports zero bytes. Measured on a live
host: a scratch checkout holding 79 KB across 21 staged files read as clean.
Any keep / reap / salvage decision taken on that answer discards real work while
reporting success.

This is the mechanical half — a NEW invocation with neither a revision nor
``--cached`` fails the PR that introduces it. Its reach is the argv literals
passed directly at a call site; an argv assembled across statements is out of an
AST walk's reach, which is what
``tests/conformance/test_dirtiness_deciders_see_index.py`` covers behaviourally.
"""

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "teatree"
# Below this the walk cannot have been looking at the real source at all.
_MIN_DIFF_CALL_SITES = 5


def _string_argv(node: ast.List) -> list[str] | None:
    """The argv as plain strings, or ``None`` when any element is not a literal string.

    A non-literal element (an f-string range, a ref variable) IS the revision this
    walk looks for, so such an argv is never a candidate.
    """
    tokens: list[str] = []
    for element in node.elts:
        if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
            return None
        tokens.append(element.value)
    return tokens


def _diff_argv(tokens: list[str]) -> list[str] | None:
    """``tokens`` from past the ``diff`` verb, or ``None`` when it is not a git-diff argv."""
    head = tokens[1:] if tokens[:1] == ["git"] else tokens
    return head[1:] if head[:1] == ["diff"] else None


def _is_index_blind(args: list[str]) -> bool:
    """A diff argv carrying neither a revision nor ``--cached`` compares against the index alone."""
    if "--cached" in args or "--staged" in args:
        return False
    before_pathspec = args[: args.index("--")] if "--" in args else args
    return not any(not token.startswith("-") for token in before_pathspec)


def _call_site_diff_argvs(source: str) -> list[list[str]]:
    """Every literal git-diff argv passed directly as a call argument in ``source``."""
    argvs: list[list[str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for candidate in (*node.args, *(keyword.value for keyword in node.keywords)):
            if not isinstance(candidate, ast.List):
                continue
            tokens = _string_argv(candidate)
            args = _diff_argv(tokens) if tokens is not None else None
            if args is not None:
                argvs.append(args)
    return argvs


def _walk() -> tuple[list[str], int]:
    offenders: list[str] = []
    seen = 0
    for path in sorted(_SRC.rglob("*.py")):
        for args in _call_site_diff_argvs(path.read_text(encoding="utf-8")):
            seen += 1
            if _is_index_blind(args):
                offenders.append(f"{path.relative_to(_SRC.parent)}: git diff {' '.join(args)}")
    return offenders, seen


class TestNoIndexBlindGitDiffInSource:
    def test_walk_reaches_the_real_call_sites(self) -> None:
        _, seen = _walk()

        assert seen >= _MIN_DIFF_CALL_SITES, f"the walk only reached {seen} git-diff call sites — it is broken"

    def test_no_source_call_site_diffs_against_the_index_alone(self) -> None:
        offenders, _ = _walk()

        assert offenders == [], "anchor these on a revision (`git diff HEAD`) or on `--cached`"

    def test_the_walk_catches_a_planted_index_blind_diff(self) -> None:
        planted = _call_site_diff_argvs('git.run(repo=repo, args=["diff", "--name-only"])')

        assert [_is_index_blind(args) for args in planted] == [True]

    def test_a_revision_and_a_pathspec_are_told_apart(self) -> None:
        assert not _is_index_blind(["--quiet", "HEAD", "--", "some/path.py"])
        assert _is_index_blind(["--quiet", "--", "some/path.py"])
