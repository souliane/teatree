"""The per-action-class approval dial the taint-floor seam consults (#119, PR-11).

#116 shipped the taint-floor seam (:func:`~teatree.core.models.approval_policy.approval_policy`)
with an empty always-ASK dial. This is the real dial it injects: a per-action-class
trust table (DB-home ``ConfigSetting`` row ``approval_dial``, a ``{class: "ask"|"auto"}``
JSON dict, overlay-scope layered on global) consulted only on the owner-taint branch
the floor already cleared.

:func:`policy_dial` is the injected ``Dial``. It returns AUTO_APPROVE for a class only
when THREE independent gates all pass:

1. the class is not a **never-fades** class (:data:`NEVER_FADES`) — a non-templated
    public issue/PR body or any change to a gate/permission/credential/detector never
    graduates, whatever the config says. (The other never-fades case — free-text external
    posts from untrusted input — is already handled by the taint floor ABOVE the dial.)
2. the operator has **graduated** the class to ``auto`` in the ``approval_dial`` table
    (ships empty → every class ASK → the dial is inert at ship);
3. the class's trailing-window **metrics** have not breached
    (:func:`~teatree.core.models.approval_metrics.metrics_breached`) — a decline / defect
    escape / rework auto-re-tightens a graduated class back to ASK.

Any failure, or any error reading the table/metrics, fails **closed** to ASK — the dial
can only ever WIDEN the owner-taint branch, never bypass the floor, and never guess AUTO.

Graduation is an audited auto-answer, NOT a bypass: :func:`auto_answer_by_policy`
consumes the recorded :class:`DeferredQuestion` single-use with ``resolved_via='policy'``
and writes a :class:`DeferredQuestionAudit` row, so ``ask_ratification`` / ``ask_keep``
still satisfy their structural "consumed answered question" guards — only the answer was
recorded by policy instead of a human.
"""

import logging
import os

from teatree.core.models.approval_metrics import metrics_breached
from teatree.core.models.approval_policy import GATE_OR_POLICY_CHANGE, PUBLIC_ISSUE_CREATE, Decision
from teatree.core.models.config_setting import GLOBAL_SCOPE, ConfigSetting
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.models.trust_level import TrustLevel

logger = logging.getLogger(__name__)

#: The DB-home ``ConfigSetting`` key holding the ``{action_class: "ask"|"auto"}`` table.
DIAL_CONFIG_KEY = "approval_dial"

#: The resolver/approver id an audited policy auto-answer is stamped with.
POLICY_RESOLVER = "policy"

#: Classes that NEVER graduate — the dial returns ASK for them regardless of the
#: configured trust or metrics: a non-templated public issue/PR body
#: (``public_issue_create``) and any change to a gate/permission/credential/detector
#: (``gate_or_policy_change``). The third never-fades case in the #119 scope —
#: free-text external posts from untrusted input — is floored ABOVE the dial by the
#: taint check in :func:`~teatree.core.models.approval_policy.approval_policy`.
NEVER_FADES: frozenset[str] = frozenset({PUBLIC_ISSUE_CREATE, GATE_OR_POLICY_CHANGE})


def policy_dial(action_class: str) -> Decision:
    """The injected ``Dial`` — :func:`effective_decision` for the active overlay scope."""
    return effective_decision(action_class)


def effective_decision(action_class: str, *, overlay: str | None = None) -> Decision:
    """The #119 dial: AUTO_APPROVE only for a graduated, un-breached, fade-able class.

    Fails closed to ASK on a never-fades class, an ungraduated class, a metric breach,
    or any error reading the table/metrics — the dial only widens the owner-taint branch.
    """
    if action_class in NEVER_FADES:
        return Decision.ASK
    try:
        graduated = configured_trust(action_class, overlay=overlay) is TrustLevel.AUTO
        breached = graduated and metrics_breached(action_class)
    except Exception:  # noqa: BLE001 — an unreadable table/metrics fails CLOSED to ASK, never AUTO.
        logger.debug("approval_dial: table/metrics read failed for %r — defaulting to ASK", action_class)
        return Decision.ASK
    return Decision.AUTO_APPROVE if graduated and not breached else Decision.ASK


def configured_trust(action_class: str, *, overlay: str | None = None) -> TrustLevel:
    """The operator-configured trust for *action_class* (default ASK when unset/invalid)."""
    raw = resolve_dial_table(overlay=overlay).get(action_class)
    if raw is None:
        return TrustLevel.ASK
    try:
        return TrustLevel(str(raw).strip().lower())
    except ValueError:
        return TrustLevel.ASK  # an out-of-vocabulary stored value fails closed to ASK


def resolve_dial_table(*, overlay: str | None = None) -> dict[str, str]:
    """The resolved ``{class: trust}`` table — global ``ConfigSetting`` rows, overlay on top.

    Mirrors the DB-home resolver's global-then-overlay layering: a per-overlay
    ``approval_dial`` row's classes override the global row's. *overlay* defaults to
    the active ``T3_OVERLAY_NAME`` scope.
    """
    scope = overlay if overlay is not None else os.environ.get("T3_OVERLAY_NAME", "")
    merged: dict[str, str] = {}
    global_row = ConfigSetting.objects.get_effective(DIAL_CONFIG_KEY, GLOBAL_SCOPE)
    if isinstance(global_row, dict):
        merged.update(global_row)
    if scope and scope != GLOBAL_SCOPE:
        overlay_row = ConfigSetting.objects.get_effective(DIAL_CONFIG_KEY, scope)
        if isinstance(overlay_row, dict):
            merged.update(overlay_row)
    return {str(key): str(value) for key, value in merged.items()}


def auto_answer_by_policy(question: DeferredQuestion, answer: str) -> DeferredQuestion | None:
    """Consume *question* single-use with *answer* by policy — an audited auto-answer.

    Stamps ``resolved_via='policy'`` and writes the :class:`DeferredQuestionAudit`
    receipt (``resolver_id='policy'``), so the graduation leaves an audit row and the
    caller's structural "consumed answered question" guard still holds. Returns the
    consumed row, or ``None`` when it was already resolved (a concurrent answer won).
    """
    row = question.apply_answer(answer, resolved_via=DeferredQuestion.ResolvedVia.POLICY)
    if row is not None:
        DeferredQuestionAudit.objects.create(
            question=row,
            action="answered",
            answer_text=answer,
            resolver_id=POLICY_RESOLVER,
        )
    return row
