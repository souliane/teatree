"""Declarative generator for the behavioral-eval corpus.

A scenario plus its three anti-vacuous fixtures (``_pass`` / ``_fail`` /
``_noop``) must stay mutually consistent: the positive matcher must match the
``_pass`` tool call and miss the ``_fail`` one; a negative matcher must miss the
``_pass`` call and match the ``_fail`` one; the ``_noop`` transcript must carry
no tool call so an only-negative scenario surfaces RED. Hand-maintaining that
consistency across hundreds of scenarios is error-prone, so the corpus is
declared once as :class:`Scenario` rows and emitted by :func:`emit_scenario`
into the YAML the loader reads and the JSONL fixtures the anti-vacuous gate
replays. ``tests/agent_behavior/replay/test_corpus_generation.py`` re-runs the emitter and
asserts the committed files match, so the declaration is the single source of
truth and drift is a test failure.
"""

from scripts.eval.corpus_gen.emit import emit_catalog, emit_scenario
from scripts.eval.corpus_gen.model import (
    Branch,
    Call,
    Expect,
    Scenario,
    any_of,
    fixture_stream,
    match,
    negative,
    positive,
    scenario_yaml,
)

__all__ = [
    "Branch",
    "Call",
    "Expect",
    "Scenario",
    "any_of",
    "emit_catalog",
    "emit_scenario",
    "fixture_stream",
    "match",
    "negative",
    "positive",
    "scenario_yaml",
]
