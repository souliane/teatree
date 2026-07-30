"""Quality gate: no concrete Claude model id is pinned outside the allowlist (§3a #1, §7 #7).

The model-evolution goal requires that adopting/swapping a model is a config change,
not a code edit. A concrete dated model-id string (``claude-haiku-4-5``,
``claude-opus-4-8[1m]``) is only legitimate in a small, enumerated set of files:

*   ``src/teatree/agents/model_tiering.py`` — THE dispatch-resolution single source
    of truth (:data:`TIER_MODELS`);
*   two NON-dispatch model-capability pins (``llm/rate_limits.py``'s probe model,
    which cannot import the tier catalog without an upward layer edge;
    ``core/autocompact_advisory.py``'s harness native-1M set, a decoded harness fact
    that is deliberately NOT family-shaped);
*   ``core/cost.py`` — HISTORICAL usage-record parsing prose (it must keep pricing
    yesterday's recorded ids);
*   the eval-corpus scenarios, whose deliberate per-scenario pins are reproducibility
    choices.

Everywhere else — production dispatch, the eval lane, CLI help, docs, workflows —
must reference an abstract TIER / family alias, or DERIVE from ``TIER_MODELS``.

The check scans BOTH directions: a model id in a non-allowlisted file is red (a new
pin), and an allowlist entry that no longer carries one is red (a stale exemption).

It also scans WIDE, on two surfaces, because that is where the stale pins actually
hide — inside a longer literal, not as a whole one:

*   PYTHON — any model id appearing ANYWHERE inside a string literal, so a
    ``help=`` string, a docstring example, or an inline comment-in-a-docstring is
    caught, not just a bare ``MODEL = "claude-…"`` assignment;
*   DOCS / WORKFLOWS — the same ids in the surfaces that carry live, copy-pasteable
    examples (BLUEPRINT, READMEs, ``docs/``, skills, GitHub workflows), including
    the GENERATED CLI reference, so a stale ``--models`` help example is caught at
    its source and again in the regenerated doc.
"""

# test-path: cross-cutting — a whole-tree quality gate, mirrors no single module.

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# A concrete Claude model id: ``claude-<family>-<version...>`` optionally
# ``[1m]``-suffixed. Matched as a SUBSTRING (``search``), so an id buried inside a
# longer literal or a paragraph of prose counts — that is exactly the shape a
# generation-pinned example takes.
_MODEL_ID = re.compile(r"claude-(?:opus|sonnet|haiku|fable|mythos)-[0-9][0-9a-z.\-]*(?:\[1m\])?", re.IGNORECASE)

# Python sources whose STRING LITERALS are scanned.
_PY_GLOBS: tuple[str, ...] = ("src/teatree/**/*.py", "hooks/scripts/*.py")

# Prose/config surfaces scanned as RAW TEXT — the ones carrying live examples a
# reader copy-pastes, so a stale id there misleads as badly as one in code.
_DOC_GLOBS: tuple[str, ...] = (
    "BLUEPRINT.md",
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "docs/**/*.md",
    "evals/*.md",
    "evals/scenarios/*.yaml",
    "skills/**/*.md",
    ".github/workflows/*.yml",
)

# Files permitted to carry a concrete model id, repo-root-relative. Adding an entry
# needs a real justification: a new home for concrete ids fights the model-evolution
# goal. Each entry's reason is in the module docstring above.
_ALLOWLIST: frozenset[str] = frozenset(
    {
        "src/teatree/agents/model_tiering.py",
        "src/teatree/llm/rate_limits.py",
        "src/teatree/core/autocompact_advisory.py",
        "src/teatree/core/cost.py",
        "evals/scenarios/e2e_review.yaml",
    }
)


def _model_ids_in_python(path: Path) -> set[str]:
    """Every model id appearing inside a string literal of *path* (substring match)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover — the tree is syntactically valid in CI
        return set()
    return {
        match
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        for match in _MODEL_ID.findall(node.value)
    }


def _files_with_model_ids() -> dict[str, set[str]]:
    """Map each repo-relative scanned file → the set of concrete model ids it carries."""
    found: dict[str, set[str]] = {}
    for glob in _PY_GLOBS:
        for path in sorted(_REPO_ROOT.glob(glob)):
            ids = _model_ids_in_python(path)
            if ids:
                found[path.relative_to(_REPO_ROOT).as_posix()] = ids
    for glob in _DOC_GLOBS:
        for path in sorted(_REPO_ROOT.glob(glob)):
            ids = set(_MODEL_ID.findall(path.read_text(encoding="utf-8")))
            if ids:
                found[path.relative_to(_REPO_ROOT).as_posix()] = ids
    return found


def test_no_hardcoded_model_id_outside_the_allowlist() -> None:
    offenders = {rel: ids for rel, ids in _files_with_model_ids().items() if rel not in _ALLOWLIST}
    assert offenders == {}, (
        "Concrete Claude model-id(s) pinned outside the allowlist — reference an abstract tier, a "
        "family alias (opus/sonnet/haiku), or derive from agents.model_tiering.TIER_MODELS "
        f"instead: {offenders}"
    )


def test_every_allowlist_entry_still_carries_a_concrete_id() -> None:
    # Reverse direction: an allowlisted file that no longer holds a model id is a
    # STALE entry — the id moved/was removed, so the exemption must go too, or the
    # allowlist rots into a silent hole.
    carriers = set(_files_with_model_ids())
    stale = {entry for entry in _ALLOWLIST if entry not in carriers}
    assert stale == set(), (
        f"Stale allowlist entries — these files no longer hold a concrete model id, remove them: {stale}"
    )


def test_guard_detects_an_id_buried_inside_a_longer_literal(tmp_path: Path) -> None:
    # Anti-vacuity: the WIDENING is the point. A whole-literal matcher would miss
    # every one of these — a `help=` example, a docstring mention, a prose line.
    module = tmp_path / "sample.py"
    module.write_text('HELP = "e.g. claude-opus-4-8@xhigh, one per column"\n', encoding="utf-8")
    assert _model_ids_in_python(module) == {"claude-opus-4-8"}


def test_guard_ignores_a_literal_with_no_model_id(tmp_path: Path) -> None:
    module = tmp_path / "clean.py"
    module.write_text('HELP = "e.g. opus@xhigh, one per column"\n', encoding="utf-8")
    assert _model_ids_in_python(module) == set()
