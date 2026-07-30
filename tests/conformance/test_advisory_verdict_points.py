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

import importlib
import re
from pathlib import Path
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
    """

    def test_each_named_point_reads_the_surface_axis(self) -> None:
        missing = [
            name for name in ADVISORY_EXEMPT_VERDICT_POINTS + ADVISORY_EXEMPT_CONSUMERS if not _reads_the_axis(name)
        ]
        assert missing == []


def _reads_the_axis(dotted: str) -> bool:
    """Whether the module owning *dotted* consults the advisory/surface axis at all."""
    source = Path(str(_owning_module(dotted).__file__)).read_text(encoding="utf-8")
    return "is_advisory" in source or "advisory" in source


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
