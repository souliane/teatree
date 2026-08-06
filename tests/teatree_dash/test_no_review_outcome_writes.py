"""No dashboard control may create, alter or satisfy a review outcome (#4085).

The maker≠checker gate is the one thing standing between the owner and self-merged
unreviewed code. Enqueuing a review is safe; asserting its RESULT is not — so the whole
``teatree.dash`` package is walked, not just the one view that #4085 added, because the
next button someone writes must turn this red too.

The package holds no reference to either outcome model at all, which is the strongest
form of the claim and the one asserted: a module that cannot NAME a ``ReviewVerdict``
cannot rebind one and save it either. A future legitimate READ would have to import the
model, at which point this guard degrades to the write-call rule below — re-derive the
claim then rather than widening the allowlist.
"""

# test-path: cross-cutting — one contract over every module in the dash package

import ast
from pathlib import Path

from django.test import TestCase
from django.urls import reverse

from teatree.core.models.merge_clear import MergeClear
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.dash.task_actions import ENQUEUEABLE_PHASES
from tests.factories import TicketFactory

_DASH = Path(__file__).resolve().parents[2] / "src/teatree/dash"

#: Model names whose rows record a review OUTCOME.
OUTCOME_MODELS: frozenset[str] = frozenset({"ReviewVerdict", "MergeClear"})

#: Call attributes that mutate. ``record`` / ``issue`` are the subsystem's own
#: verdict-writing and CLEAR-issuing verbs, not ORM ones.
WRITE_CALLS: frozenset[str] = frozenset(
    {
        "create",
        "get_or_create",
        "update_or_create",
        "bulk_create",
        "bulk_update",
        "save",
        "update",
        "delete",
        "record",
        "issue",
    }
)


def outcome_model_offenders(source: str, label: str) -> list[str]:
    """Every line in *source* that reaches an outcome model: an import, a lazy resolve, a write."""
    tree = ast.parse(source, filename=label)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt | ast.expr):
            continue
        reason = _offence(node)
        if reason:
            offenders.append(f"{label}:{node.lineno}: {reason}")
    return offenders


def _offence(node: ast.stmt | ast.expr) -> str:
    if isinstance(node, ast.ImportFrom | ast.Import):
        named = sorted({alias.name.rsplit(".", 1)[-1] for alias in node.names} & OUTCOME_MODELS)
        return f"imports {', '.join(named)}" if named else ""
    if isinstance(node, ast.Call):
        return _call_offence(node)
    return ""


def _call_offence(node: ast.Call) -> str:
    lazy = _lazily_resolved_model(node)
    if lazy:
        return f"resolves {lazy} lazily"
    if not _names_an_outcome_model(node.func):
        return ""
    attr = node.func.attr if isinstance(node.func, ast.Attribute) else ""
    if attr in WRITE_CALLS or isinstance(node.func, ast.Name):
        return f"writes via {ast.unparse(node.func)}"
    return ""


def _lazily_resolved_model(node: ast.Call) -> str:
    """The outcome model a ``get_model("core", "ReviewVerdict")``-shaped call names, if any."""
    called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
    if called != "get_model":
        return ""
    named = [
        arg.value
        for arg in node.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value in OUTCOME_MODELS
    ]
    return named[0] if named else ""


def _names_an_outcome_model(func: ast.expr) -> bool:
    """Whether the called expression's receiver chain mentions an outcome model."""
    if isinstance(func, ast.Attribute) and func.attr in OUTCOME_MODELS:
        return True
    return any(isinstance(part, ast.Name) and part.id in OUTCOME_MODELS for part in ast.walk(func))


def test_no_dash_module_reaches_a_review_verdict_or_a_merge_clear() -> None:
    offenders = [
        offence
        for path in sorted(_DASH.rglob("*.py"))
        for offence in outcome_model_offenders(path.read_text(encoding="utf-8"), str(path.relative_to(_DASH)))
    ]
    assert not offenders, (
        "a dashboard module reaches a review outcome — the maker≠checker gate is the one "
        "thing standing between the owner and self-merged unreviewed code:\n" + "\n".join(offenders)
    )


def test_the_detector_flags_every_shape_a_write_could_take() -> None:
    # Without this the walk above is unfalsifiable: a detector that flags nothing
    # passes over a clean tree exactly like a correct one does.
    shapes = {
        "import": "from teatree.core.models.review_verdict import ReviewVerdict",
        "manager create": "ReviewVerdict.objects.create(verdict='merge_safe')",
        "bare construction": "MergeClear(slug='x/y')",
        "instance save": "row = ReviewVerdict.objects.first()\nReviewVerdict.objects.filter(pk=1).update(verdict='x')",
        "lazy resolve": "apps.get_model('core', 'MergeClear')",
    }
    for name, source in shapes.items():
        assert outcome_model_offenders(source, "probe.py"), f"the {name} shape is not detected"


def test_the_detector_leaves_an_ordinary_dashboard_module_alone() -> None:
    clean = "from teatree.core.models.task import Task\nTask.objects.create(phase='reviewing')"
    assert outcome_model_offenders(clean, "probe.py") == []


class EnqueueingLeavesEveryOutcomeStoreEmptyTestCase(TestCase):
    """The package walk is static; this is the same claim executed."""

    def test_clicking_every_button_records_no_verdict_and_no_clear(self) -> None:
        ticket = TicketFactory()
        for phase in ENQUEUEABLE_PHASES:
            response = self.client.post(
                reverse("dash:task_action", args=[ticket.pk]), {"phase": phase}, headers={"hx-request": "true"}
            )
            assert response.status_code == 200
        assert not ReviewVerdict.objects.exists()
        assert not MergeClear.objects.exists()
