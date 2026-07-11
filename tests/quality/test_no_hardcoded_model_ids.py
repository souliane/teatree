"""Quality gate: no concrete Claude model id is hardcoded outside the allowlist (§3a #1, §7 #7).

The model-evolution goal requires that adopting/swapping a model is a config change,
not a code edit. A concrete dated model-id string literal (``claude-haiku-4-5``,
``claude-opus-4-8[1m]``) is only legitimate in a small, enumerated set of files:

*   ``agents/model_tiering.py`` — THE dispatch-resolution single source of truth
    (:data:`TIER_MODELS` + the pydantic_ai catalog);
*   the eval-lane pins (``eval/transcript.py``, ``eval/loader.py``, ``eval/models.py``,
    ``eval/api_runner.py``) — the eval lane keeps its own concrete pins (Unit 3);
*   two pre-existing NON-dispatch model-capability pins (``llm/rate_limits.py`` probe
    model; ``core/autocompact_advisory.py`` 1M-context capability set) that key on a
    specific model for a reason other than dispatch. (These were NOT enumerated in the
    Fable design's frozen allowlist — flagged for the integrator to reconcile.)

Everywhere else — production phase dispatch, the aux one-shot call sites — must
reference an abstract TIER and resolve through the seam. The check scans in BOTH
directions: a bare model-id literal in a NON-allowlisted file is red (a new
hardcode), and an allowlist entry that no longer carries ANY bare model-id literal
is red (a stale allowlist entry — remove it). Prose mentions inside docstrings /
comments do not count: only a string literal that IS a bare model id is a hardcode.
"""

# test-path: cross-cutting — a whole-tree quality gate, mirrors no single module.

import ast
import re
from pathlib import Path

import teatree

# A bare concrete Claude model id: ``claude-<family>-<version...>`` optionally
# ``[1m]``-suffixed. Matched with ``fullmatch`` against a STRIPPED string literal,
# so a docstring/comment that merely mentions an id (a longer string) never trips
# it — only a literal that IS the id (the hardcoding shape).
_MODEL_ID = re.compile(r"claude-(?:opus|sonnet|haiku|fable)-[0-9][0-9a-z.\-]*(?:\[1m\])?", re.IGNORECASE)

# Files permitted to carry a bare concrete model-id literal (relative to the
# ``teatree`` package root). Adding an entry needs a real justification: a new
# home for concrete ids fights the model-evolution goal.
_ALLOWLIST: frozenset[str] = frozenset(
    {
        # THE dispatch-resolution single source of truth.
        "agents/model_tiering.py",
        # Eval-lane pins (Unit 3): the eval lane keeps its own concrete ids.
        "eval/transcript.py",
        "eval/loader.py",
        "eval/models.py",
        "eval/api_runner.py",
        # Pre-existing NON-dispatch capability pins (not enumerated by the design's
        # frozen allowlist; kept green because they key on a specific model for a
        # non-dispatch reason, not a hardcoded spawn target).
        "llm/rate_limits.py",
        "core/autocompact_advisory.py",
    }
)


def _src_root() -> Path:
    return Path(teatree.__file__).resolve().parent


def _files_with_bare_model_id_literals() -> dict[str, set[str]]:
    """Map each ``teatree``-relative ``.py`` file → the set of bare model-id literals it holds."""
    root = _src_root()
    found: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — the tree is syntactically valid in CI
            continue
        literals = {
            node.value.strip()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _MODEL_ID.fullmatch(node.value.strip())
        }
        if literals:
            found[str(path.relative_to(root))] = literals
    return found


def test_no_hardcoded_model_id_outside_the_allowlist() -> None:
    offenders = {rel: ids for rel, ids in _files_with_bare_model_id_literals().items() if rel not in _ALLOWLIST}
    assert offenders == {}, (
        "Concrete Claude model-id literal(s) hardcoded outside the allowlist — resolve through the "
        f"tier seam (agents.model_tiering.resolve_tier) instead: {offenders}"
    )


def test_every_allowlist_entry_still_carries_a_concrete_id() -> None:
    # Reverse direction: an allowlisted file that no longer holds ANY bare
    # model-id literal is a STALE entry — the id moved/was removed, so the
    # exemption must go too, or the allowlist rots into a silent hole.
    carriers = set(_files_with_bare_model_id_literals())
    stale = {entry for entry in _ALLOWLIST if entry not in carriers}
    assert stale == set(), (
        f"Stale allowlist entries — these files no longer hold a concrete model id, remove them: {stale}"
    )
