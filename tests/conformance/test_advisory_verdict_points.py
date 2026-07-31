"""The advisory-surface exemption is enumerated by NAME, and every name resolves.

The prose describing this exemption counted its verdict points twice — "at all
three aggregation points", then "four" — and both counts were wrong, because a
count goes stale silently: adding a lane leaves a sentence that still parses and
still reads like a covered invariant.

:data:`~teatree.eval.surface.ADVISORY_EXEMPT_VERDICT_POINTS` replaces the count
with a named list, and this module makes that list load-bearing rather than
decorative: every symbol must resolve (a renamed or deleted lane reds here, not in
production), every symbol must actually consult the surface, and the docs must name
the points rather than counting them again (souliane/teatree#3855,
souliane/teatree#3921).
"""

import ast
import importlib
import re
from pathlib import Path
from textwrap import dedent
from types import ModuleType

from teatree.eval.surface import ADVISORY_EXEMPT_CONSUMERS, ADVISORY_EXEMPT_VERDICT_POINTS

#: The prose that must name the verdict points, never count them.
_DOCS = ("BLUEPRINT.md", "evals/README.md")

#: The counting idiom the named list exists to retire — "three aggregation points",
#: "four verdict points", and every other number-plus-noun spelling of the same claim.
_COUNTED = re.compile(
    r"\b(two|three|four|five|six|seven|\d+)\s+(aggregation|verdict|gating)\s+points?\b",
    re.IGNORECASE,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _owning_module(dotted: str) -> ModuleType:
    """The longest importable module prefix of ``pkg.mod.attr`` / ``pkg.mod.Class.attr``."""
    parts = dotted.split(".")
    for split in range(len(parts) - 1, 0, -1):
        try:
            return importlib.import_module(".".join(parts[:split]))
        except ModuleNotFoundError:
            continue
    msg = f"no importable module prefix in {dotted!r}"
    raise AssertionError(msg)


def _resolve(dotted: str) -> object:
    """Resolve ``pkg.mod.attr`` or ``pkg.mod.Class.attr`` to the named object."""
    module = _owning_module(dotted)
    obj: object = module
    for attr in dotted.removeprefix(f"{module.__name__}.").split("."):
        obj = getattr(obj, attr)
    return obj


class TestEveryNamedVerdictPointResolves:
    """A renamed or deleted verdict point must red HERE, not in a metered CI leg."""

    def test_the_list_is_not_empty(self) -> None:
        assert ADVISORY_EXEMPT_VERDICT_POINTS

    def test_every_verdict_point_resolves(self) -> None:
        assert [_resolve(name) for name in ADVISORY_EXEMPT_VERDICT_POINTS]

    def test_every_consumer_resolves(self) -> None:
        assert [_resolve(name) for name in ADVISORY_EXEMPT_CONSUMERS]


class TestEveryNamedPointConsultsTheSurface:
    """Resolving is not enough — the symbol's module must actually read the axis.

    A module-source check rather than a behavioural one: each lane's BEHAVIOUR is
    pinned by ``tests/teatree_cli/eval/test_advisory_surface.py``, while this asserts
    the named list cannot drift into naming a point that never applies the exemption.

    The check reads CODE, never prose. A substring scan over the raw source said
    yes to any module whose docstring happened to contain the word — and
    ``teatree.cli.eval.all``'s docstring says it about a different lane entirely
    ("judge-only is advisory"), so that lane's whole exemption could be deleted,
    import and all, with this module still green. Deleting the behaviour must red
    here; :class:`TestTheCheckReadsCodeNotProse` is what keeps that true.
    """

    def test_each_named_point_reads_the_surface_axis(self) -> None:
        missing = [
            name for name in ADVISORY_EXEMPT_VERDICT_POINTS + ADVISORY_EXEMPT_CONSUMERS if not _reads_the_axis(name)
        ]
        assert missing == []


#: The predicate the in-process lanes call on a spec.
_AXIS_PREDICATE = "is_advisory"

#: The serialized flag the artifact-reading lanes (``green_proof``,
#: ``ci_eval_heal_advance``) read off a row, having no spec to ask.
_AXIS_FLAG = "advisory"


def _docstring_constants(tree: ast.AST) -> set[int]:
    """The ``id()`` of every docstring node — the prose this check must not count."""
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = node.body[0] if node.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            docstrings.add(id(first.value))
    return docstrings


def _axis_references(source: str) -> set[str]:
    """Every EXECUTABLE reference to the advisory axis in *source*.

    Comments never reach the AST and docstrings are excluded by node identity, so
    only real code counts — a binding, attribute, keyword, or mapping key named
    :data:`_AXIS_PREDICATE` or :data:`_AXIS_FLAG`.
    """
    tree = ast.parse(source)
    docstrings = _docstring_constants(tree)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name)
        elif (isinstance(node, ast.keyword) and node.arg) or isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            names.add(node.value)
    return names & {_AXIS_PREDICATE, _AXIS_FLAG}


def _reads_the_axis(dotted: str) -> bool:
    """Whether the module owning *dotted* consults the advisory/surface axis in CODE."""
    source = Path(str(_owning_module(dotted).__file__)).read_text(encoding="utf-8")
    return bool(_axis_references(source))


class TestTheCheckReadsCodeNotProse:
    """The guard on the guard: prose must not satisfy the exemption check.

    Without these, ``_reads_the_axis`` can quietly relax back into a substring
    scan and every named point passes on a docstring alone.
    """

    def test_a_docstring_alone_is_not_a_reference(self) -> None:
        # The exact shape that made the substring scan vacuous: `teatree.cli.eval.all`
        # says this about the judge-only lane, not the interactive surface.
        source = dedent(
            '''
            """The judge-only lane is advisory; matcher lanes gate CI."""

            def gated(results):
                return any(not r.passed for r in results)
            '''
        )
        assert _axis_references(source) == set()

    def test_a_comment_alone_is_not_a_reference(self) -> None:
        source = dedent(
            """
            def gated(results):
                # advisory scenarios are reported, never gating
                return any(not r.passed for r in results)
            """
        )
        assert _axis_references(source) == set()

    def test_a_merely_similar_name_is_not_a_reference(self) -> None:
        source = dedent(
            """
            def gated(results):
                advisory_detail = ""
                return advisory_detail
            """
        )
        assert _axis_references(source) == set()

    def test_calling_the_predicate_is_a_reference(self) -> None:
        source = dedent(
            """
            from teatree.eval.surface import is_advisory

            def gated(results):
                return any(not r.passed and not is_advisory(r.spec) for r in results)
            """
        )
        assert _axis_references(source) == {_AXIS_PREDICATE}

    def test_reading_the_serialized_flag_is_a_reference(self) -> None:
        source = dedent(
            """
            def gating_rows(rows):
                return [row for row in rows if not row.get("advisory")]
            """
        )
        assert _axis_references(source) == {_AXIS_FLAG}


class TestTheDocsNameRatherThanCount:
    """The counting idiom is what went stale twice; it must not come back."""

    def test_no_doc_counts_the_verdict_points(self) -> None:
        offenders = [
            f"{doc}: {match.group(0)}"
            for doc in _DOCS
            for match in _COUNTED.finditer((_repo_root() / doc).read_text(encoding="utf-8"))
        ]
        assert offenders == []

    def test_each_doc_names_the_merged_green_proof_point(self) -> None:
        # The point the counted prose omitted entirely — the one this list exists for.
        # Either spelling: `green_proof` the module, `green-proof` the CLI command.
        for doc in _DOCS:
            body = (_repo_root() / doc).read_text(encoding="utf-8").lower()
            assert "green_proof" in body or "green-proof" in body
