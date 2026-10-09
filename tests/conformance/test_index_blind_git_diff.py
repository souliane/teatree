"""No ``git diff`` in ``src/`` may decide anything from the working tree alone.

``git diff`` with no revision compares the working tree to the INDEX, so a
checkout whose entire delta is STAGED reports zero bytes. A keep / reap /
salvage decision taken on that answer would discard real work while reporting
success.

This is a forward ratchet, not a repair. Every git-diff call site in ``src/``
already anchors on a revision or ``--cached``; the walk caught no offender when
it was written, and pins that so a NEW index-blind invocation fails the PR that
introduces it.

Its reach is the argv elements resolvable at a call site — literals, and the
module-level string constants a call site names. Out of an AST walk's reach, and
covered behaviourally by ``tests/conformance/test_dirtiness_deciders_see_index.py``
instead: an argv assembled across statements, and a revision carried by a
parameter, an attribute, or an imported name.
"""

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "teatree"
# Below this the walk cannot have been looking at the real source at all.
_MIN_DIFF_CALL_SITES = 5


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, so an argv naming one stays readable.

    A call site that holds its revision in a module constant (``["diff",
    _INDEX_AWARE_BASE, ...]``) is precisely the shape this walk must judge;
    reading the ``Name`` as unresolvable would drop it from the census entirely.
    """
    constants: dict[str, str] = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)):
            continue
        if not isinstance(node.value.value, str):
            continue
        constants.update({target.id: node.value.value for target in node.targets if isinstance(target, ast.Name)})
    return constants


def _string_argv(node: ast.List, constants: dict[str, str]) -> list[str] | None:
    """The argv as plain strings, or ``None`` when an element resolves to no string.

    An element that is neither a literal nor a module constant (an f-string
    range, a parameter, an attribute) IS the revision this walk looks for, so
    such an argv is never a candidate.
    """
    tokens: list[str] = []
    for element in node.elts:
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            tokens.append(element.value)
        elif isinstance(element, ast.Name) and element.id in constants:
            tokens.append(constants[element.id])
        else:
            return None
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
    """Every resolvable git-diff argv passed directly as a call argument in ``source``."""
    tree = ast.parse(source)
    constants = _module_string_constants(tree)
    argvs: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for candidate in (*node.args, *(keyword.value for keyword in node.keywords)):
            if not isinstance(candidate, ast.List):
                continue
            tokens = _string_argv(candidate, constants)
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

    def test_a_revision_held_in_a_module_constant_is_resolved(self) -> None:
        source = 'BASE = "HEAD"\ngit.run(repo=repo, args=["diff", BASE, "--binary"])\n'

        assert [_is_index_blind(args) for args in _call_site_diff_argvs(source)] == [False]

    def test_a_module_constant_cannot_hide_an_index_blind_diff(self) -> None:
        source = 'FLAG = "--name-only"\ngit.run(repo=repo, args=["diff", FLAG])\n'

        assert [_is_index_blind(args) for args in _call_site_diff_argvs(source)] == [True]

    def test_an_unresolvable_element_still_reads_as_the_revision(self) -> None:
        assert _call_site_diff_argvs('git.run(repo=repo, args=["diff", base, "--binary"])') == []
