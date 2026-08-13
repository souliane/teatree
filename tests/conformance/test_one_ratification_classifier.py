"""Exactly ONE ratification classifier lives in the tree.

The outer loop and the directive loop both ask a human "approve to admit?", both write a
TERMINAL rejected state from the answer, and the approval dial scores the same answers.
Three readings of one question had drifted apart — the directive loop learned to read
prose while the outer loop kept a byte-duplicate eight-token frozenset that terminally
rejected everything else (souliane/teatree#4187), and the dial's own copy scored every
prose approval as a decline. A copy is invisible until it decides something wrong, so the
class is pinned here rather than the instance being fixed again.

Two legs, because a third copy can appear either way: by redeclaring the approval
lexicon, or by hand-rolling a resolver that never consults the shared one.
"""

import ast

from tests.conformance._src_tree import SRC_DIR, src_modules

#: The one module allowed to declare the approval lexicon. It lives in core, not in a
#: loop, because the approval-dial metrics read the same answers and core cannot import
#: a loop.
CANONICAL = SRC_DIR / "core" / "models" / "ratification.py"

#: Enough of the lexicon that two of them together cannot be an unrelated string set.
_APPROVAL_PROBE = frozenset({"approve", "approved", "ratify", "ratified", "admit", "accept", "lgtm"})

#: How many probe lemmas in ONE literal collection make it an approval lexicon.
_LEXICON_THRESHOLD = 2


def _string_collections(module: ast.Module) -> list[frozenset[str]]:
    """Every literal set/list/tuple of plain strings in *module*, `frozenset(...)` included."""
    collections: list[frozenset[str]] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Set | ast.List | ast.Tuple):
            continue
        strings = {item.value for item in node.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)}
        if strings:
            collections.append(frozenset(strings))
    return collections


def _declares_approval_lexicon(module: ast.Module) -> bool:
    return any(len(strings & _APPROVAL_PROBE) >= _LEXICON_THRESHOLD for strings in _string_collections(module))


def _imports_the_classifier(module: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "teatree.core.models.ratification"
        and any(alias.name == "classify_ratification_answer" for alias in node.names)
        for node in ast.walk(module)
    )


def test_the_approval_lexicon_is_declared_exactly_once() -> None:
    declaring = {path for path, module in src_modules() if _declares_approval_lexicon(module)}
    assert declaring == {CANONICAL}, f"approval lexicon copied into {sorted(declaring - {CANONICAL})}"


def test_every_ratify_phase_resolves_the_answer_through_the_shared_classifier() -> None:
    ratify_modules = [(path, module) for path, module in src_modules() if path.name == "ratify.py"]
    assert ratify_modules, "no ratify phase found — the walk is looking in the wrong place"
    hand_rolled = [path for path, module in ratify_modules if not _imports_the_classifier(module)]
    assert not hand_rolled, f"ratify phase not using the shared classifier: {sorted(hand_rolled)}"


def test_the_detector_fires_on_a_planted_copy() -> None:
    # Anti-vacuity: a green above must mean "no copy exists", never "the walk found nothing".
    planted = ast.parse('_APPROVE_TOKENS = frozenset({"approve", "approved", "yes", "ratify"})')
    assert _declares_approval_lexicon(planted)
    assert not _declares_approval_lexicon(ast.parse('_STATES = frozenset({"approve", "pending"})'))
    assert not _imports_the_classifier(ast.parse("from teatree.core.models import DeferredQuestion"))


def test_the_canonical_module_is_where_the_walk_expects_it() -> None:
    # A moved classifier otherwise reads as "the lexicon was copied", which sends the
    # next reader hunting for a duplicate that does not exist.
    assert CANONICAL.is_file(), f"canonical classifier missing at {CANONICAL}"
