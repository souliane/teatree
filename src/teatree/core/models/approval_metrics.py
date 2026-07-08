"""Per-action-class trust metrics off the SendAudit + DeferredQuestion ledgers (#119).

The per-action-class approval dial (:mod:`teatree.core.models.approval_dial`)
graduates a class to AUTO only while its trailing-window metrics stay clean; a
threshold breach auto-re-tightens the class back to ASK. The metrics are derived
from the two ledgers a graduated class actually touches:

* the :class:`~teatree.core.models.deferred_question.DeferredQuestion` ledger — the
    human touchpoints for the ratify/keep classes. A question answered by a human
    with a non-approval word is a *decline* (distrust of the auto-graduation); a
    policy auto-answer (``resolved_via='policy'``) is never a decline.
* the :class:`~teatree.core.models.send_audit.SendAudit` ledger (#117) — the
    outbound posts for the on-behalf / public-issue classes. An ``enforce``-mode
    ``DENIED`` verdict is a *defect escape* (the class authorized a send the
    allowlist then blocked); a redacted payload is *rework* (content the leak matcher
    had to scrub).

The breach predicate is safety-biased: ANY of a high human-decline rate, a defect
escape, or rework re-tightens. The never-fades classes (``public_issue_create`` /
``gate_or_policy_change``) are ASK by the dial regardless of metrics, so their
metrics are reported for the operator but never gate a decision.

Class attribution needs no schema change: a DeferredQuestion is mapped by its
``options_hash`` prefix (the ratify/keep recorders already tag it), a SendAudit by
its ``action`` field — both as data tables here, easy to extend as more call sites
route through the send-proxy.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Q
from django.utils import timezone

from teatree.core.models.approval_policy import DIRECTIVE_ADMIT, ON_BEHALF_POST, OUTER_LOOP_KEEP, PUBLIC_ISSUE_CREATE
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.send_audit import SendAudit

#: Trailing window the metrics are computed over — "interventions/week" etc.
WINDOW_DAYS = 7

#: A graduated ratify/keep class re-tightens once the human-decline rate over the
#: window exceeds this fraction of the answered questions.
DECLINE_RATE_THRESHOLD = 0.25

#: Answers that count as an approval (so their inverse is a decline). Covers both
#: the ratify vocabulary ("approve"/"yes"/…) and the keep vocabulary ("kept").
_APPROVAL_ANSWERS: frozenset[str] = frozenset(
    {"approve", "approved", "yes", "y", "1", "ratify", "admit", "ok", "kept", "keep"}
)

#: ``DeferredQuestion.options_hash`` prefix → the approval class it records for.
#: ``directive_ratify`` is the directive-admit ledger; ``outer_loop_keep`` the keep one.
_QUESTION_PREFIX_CLASSES: dict[str, str] = {
    "directive_ratify": DIRECTIVE_ADMIT,
    "outer_loop_keep": OUTER_LOOP_KEEP,
}

#: ``SendAudit.action`` → the approval class the outbound post belongs to.
_SEND_ACTION_CLASSES: dict[str, str] = {
    "post_comment": ON_BEHALF_POST,
    "review_request_post": ON_BEHALF_POST,
    "reply_to_discussion": ON_BEHALF_POST,
    "resolve_discussion": ON_BEHALF_POST,
    "issue_create": PUBLIC_ISSUE_CREATE,
    "pr_create": PUBLIC_ISSUE_CREATE,
}


@dataclass(frozen=True, slots=True)
class ClassMetrics:
    """One action class's trailing-window trust metrics (interventions / decline / escape / rework)."""

    action_class: str
    interventions: int
    resolved: int
    declines: int
    defect_escapes: int
    rework: int

    @property
    def decline_rate(self) -> float:
        return self.declines / self.resolved if self.resolved else 0.0

    @property
    def breached(self) -> bool:
        """True when any safety-biased threshold is crossed — the auto-re-tighten trigger."""
        return self.decline_rate > DECLINE_RATE_THRESHOLD or self.defect_escapes > 0 or self.rework > 0


def compute_metrics(action_class: str, *, now: datetime | None = None) -> ClassMetrics:
    """Compute *action_class*'s metrics over the trailing :data:`WINDOW_DAYS` window."""
    cutoff = (now or timezone.now()) - timedelta(days=WINDOW_DAYS)
    interventions, resolved, declines = _question_metrics(action_class, cutoff)
    defect_escapes, rework = _send_metrics(action_class, cutoff)
    return ClassMetrics(
        action_class=action_class,
        interventions=interventions,
        resolved=resolved,
        declines=declines,
        defect_escapes=defect_escapes,
        rework=rework,
    )


def metrics_breached(action_class: str, *, now: datetime | None = None) -> bool:
    """True iff *action_class*'s trailing-window metrics breach a threshold (re-tighten to ASK)."""
    return compute_metrics(action_class, now=now).breached


def _question_metrics(action_class: str, cutoff: datetime) -> tuple[int, int, int]:
    """``(interventions, resolved, declines)`` from the DeferredQuestion ledger for *action_class*."""
    prefixes = [prefix for prefix, klass in _QUESTION_PREFIX_CLASSES.items() if klass == action_class]
    if not prefixes:
        return 0, 0, 0
    prefix_filter = Q()
    for prefix in prefixes:
        prefix_filter |= Q(options_hash__startswith=f"{prefix}:")
    rows = DeferredQuestion.objects.filter(prefix_filter, created_at__gte=cutoff)
    interventions = rows.count()
    resolved = declines = 0
    for row in rows.filter(answered_at__isnull=False):
        resolved += 1
        if _is_decline(row):
            declines += 1
    return interventions, resolved, declines


def _is_decline(row: DeferredQuestion) -> bool:
    """A human answer that is not an approval word — a policy auto-answer is never a decline."""
    if row.resolved_via == DeferredQuestion.ResolvedVia.POLICY:
        return False
    return row.answer_text.strip().lower() not in _APPROVAL_ANSWERS


def _send_metrics(action_class: str, cutoff: datetime) -> tuple[int, int]:
    """``(defect_escapes, rework)`` from the SendAudit ledger for *action_class*."""
    actions = [action for action, klass in _SEND_ACTION_CLASSES.items() if klass == action_class]
    if not actions:
        return 0, 0
    rows = SendAudit.objects.filter(action__in=actions, created_at__gte=cutoff)
    defect_escapes = rows.filter(allowlist_verdict=SendAudit.Verdict.DENIED).count()
    rework = rows.filter(redaction_applied=True).count()
    return defect_escapes, rework
