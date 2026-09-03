"""Driver for the CI-eval self-healing loop (#3201 PR-3a observe + PR-3b fixer).

An operator opens a :class:`~teatree.core.models.CiEvalHealSession` for a PR branch
(``t3 eval ci-heal open``); this module advances every open session ONE FSM step
per tick, driven by the default-OFF ``ci_eval_heal`` mini-loop (or by an operator
dry-run via ``t3 eval ci-heal advance``):

* ``PENDING`` → dispatch the ``eval-ci-heal`` workflow against the branch (``$0``
    subscription credential), record the head SHA, and move to ``AWAITING_CI``.
* ``AWAITING_CI`` → poll the run (non-blocking, one bounded ``gh`` read). While it
    runs, no-op. On ``success`` → ``receive_result([])`` → GREEN. On any non-success
    conclusion, the run is NEVER greened: a ``failure`` carrying parseable behavioral
    reds moves through ``TRIAGING``; any other conclusion, or a failure whose reds
    cannot be confirmed, is an infra HALT (escalated).
* ``TRIAGING`` → GREEN when no red remains. With a red: observe-only (the default)
    HALTs + escalates; when the fixer is ARMED (:func:`~teatree.loop.ci_eval_heal_fixer.autofix_armed`
    — the ``ci_eval_heal_autofix_enabled`` DARK flag AND the loop row both on) and the
    fix budget is not exhausted, it dispatches ONE bounded autonomous fix instead
    (``begin_fix`` → propose → gate → publish → re-trigger). Budget exhausted ⇒ HALT.
* ``PUSHED`` → re-trigger the eval on the fixed branch (the loop back-edge; recovers
    a fix that pushed but crashed before re-dispatch).

**Anti-cheat invariant (non-negotiable).** A genuinely-failing eval can never be
marked green. ``GREEN`` is reachable from exactly ONE place — a run whose CI
conclusion is ``success`` (an empty red set) — and the model's ``_no_reds`` guard
independently refuses ``mark_green`` while any red remains. The fixer only PROPOSES:
the #3282 anti-cheat gate (``record_fix``) runs over the proposed diff BEFORE any
push, so a fix editing ``evals/scenarios/**`` or the eval harness
(``src/teatree/eval/**``) is REJECTED and
DISCARDED, never reaching the branch. A red, an infra failure, an unconfirmable
result, an exhausted budget, or a rejected/empty fix all terminate at ``HALTED`` and
escalate to the human via a :class:`~teatree.core.models.DeferredQuestion` (the
§17.1 invariant-9 surface: statusline / ``t3 teatree questions list`` / Slack DM).
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from django.db import transaction

from teatree.backends.github.ci_eval_client import (
    DEFAULT_CI_EVAL_REPO,
    EVAL_CI_HEAL_WORKFLOW,
    GhCiEvalClient,
    build_ci_eval_client,
)
from teatree.loop.ci_eval_heal_fixer import CiEvalHealFixer, autofix_armed, default_fixer
from teatree.types import RawAPIDict

if TYPE_CHECKING:
    from teatree.core.models import CiEvalHealSession

logger = logging.getLogger(__name__)

#: The $0 credential the observe loop always dispatches with (issue #3201): the
#: behavioral eval runs on the subscription, never a per-token metered key. Matches
#: the ``eval-ci-heal`` workflow's ``credential`` input vocabulary.
_DISPATCH_CREDENTIAL = "subscription_oauth"

#: The per-session dedup marker so one HALTED session escalates exactly once — a
#: dismissed/answered question never resurrects a fresh one (mirrors the
#: stuck-ticket escalation idiom).
_HALT_MARKER = "[ci-eval-heal-halt session={pk}]"

#: A callable that escalates a HALTED session to the human. Injected so tests can
#: spy without a DB write; the production default records a ``DeferredQuestion``.
EscalateFn = Callable[["CiEvalHealSession"], None]

#: The outcome note for an advancer that lost its step to a rival holding the same row.
_SUPERSEDED_NOTE = "superseded by a rival advancer"


@dataclass(frozen=True, slots=True)
class AdvanceOutcome:
    """One session's one-step advance result — what moved, and why."""

    pr_ref: str
    from_state: str
    to_state: str
    note: str = ""


def red_scenario_names(payload: RawAPIDict) -> list[str]:
    """The names of the scenarios a summary-json artifact grades RED.

    A red scenario carries a non-null ``triage_class`` (the discriminator
    ``teatree.eval.summary_json`` writes); a green one carries ``null``. Pure and
    total over a possibly-malformed payload — a non-list ``scenarios`` yields no
    reds rather than raising, so a bad artifact degrades to "no confirmable reds"
    (an infra halt), never to a false green.

    An ``advisory`` row is EXCLUDED: these names are the fixer-dispatch set, and an
    ``interactive``-surface red is a bundled claude CLI's ``AskUserQuestion``
    rendering change that no repo edit can fix, so naming it would burn a fixer
    agent on an unfixable target (souliane/teatree#3855, souliane/teatree#3921).
    The producer derives the flag once (:class:`teatree.eval.triage.ScenarioRecord`);
    reading it off the wire keeps this module inside its ``teatree.loop`` boundary
    instead of re-deriving the exemption from a copied surface literal. A row
    missing the flag reads as GATING — an older artifact is never silently exempted.
    """
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list):
        return []
    names: list[str] = []
    for raw in scenarios:
        if not isinstance(raw, dict):
            continue
        record = cast("RawAPIDict", raw)
        if record.get("triage_class") is None or bool(record.get("advisory")):
            continue
        names.append(str(record.get("name", "")))
    return names


def _match_run(runs: Iterable[RawAPIDict], *, head_sha: str) -> RawAPIDict | None:
    """The newest run whose head SHA is the one this session dispatched, else ``None``.

    ``list_runs`` is newest-first, so the first SHA match is the run this session's
    ``trigger`` keyed on — never a stale earlier run for the same branch.
    """
    for run in runs:
        if str(run.get("headSha") or "") == head_sha:
            return run
    return None


def _load_json(path: Path) -> RawAPIDict:
    import json  # noqa: PLC0415 — tiny, keep the module import surface small

    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _download_reds(client: GhCiEvalClient, *, run_id: int | None, head_sha: str) -> list[str] | None:
    """Download the ``eval-heal-<sha>`` artifact and parse its reds, or ``None`` on any failure.

    ``None`` means "the reds could not be confirmed" (no run id / SHA, a download
    error, or an artifact carrying no JSON) — the caller treats that as an infra
    HALT, never as an empty (green) red set. A full-suite run drops one JSON; a
    targeted subset drops one per scenario, so every JSON is read.
    """
    import tempfile  # noqa: PLC0415 — scratch dir only on the download path

    from teatree.utils.run import CommandFailedError  # noqa: PLC0415 — deferred: subprocess-error type

    if run_id is None or not head_sha:
        return None
    with tempfile.TemporaryDirectory() as scratch:
        dest = Path(scratch)
        try:
            client.download_artifact(run_id, name=f"eval-heal-{head_sha}", dest_dir=dest)
        except (CommandFailedError, FileNotFoundError) as exc:
            logger.warning("ci_eval_heal: could not download eval-heal-%s: %s", head_sha, exc)
            return None
        artifacts = sorted(dest.rglob("*.json"))
        if not artifacts:
            return None
        return [name for artifact in artifacts for name in red_scenario_names(_load_json(artifact))]


def _advance_owned(
    session: "CiEvalHealSession", transition: Callable[["CiEvalHealSession"], None]
) -> "CiEvalHealSession | None":
    """Apply one FSM transition under a row lock; ``None`` when the row moved under us.

    The loop tick and ``t3 eval ci-heal advance`` both call :func:`advance_open_sessions`,
    so two advancers routinely hold the same scanned row, and django-fsm guards on the
    IN-MEMORY ``state`` — both pass their source check, and both act. The generation
    compared is ``updated_at``, not ``state``: the fix branch returns to ``AWAITING_CI``,
    so a matching state does not prove the row stood still.
    """
    from teatree.core.models import CiEvalHealSession  # noqa: PLC0415 — deferred: ORM needs the app registry

    with transaction.atomic():
        owned = (
            CiEvalHealSession.objects.select_for_update().filter(pk=session.pk, updated_at=session.updated_at).first()
        )
        if owned is None:
            return None
        transition(owned)
        owned.save()
        return owned


def _fire_workflow(session: "CiEvalHealSession", *, client: GhCiEvalClient) -> bool:
    """Dispatch the eval workflow for the claimed step; ``False`` when the forge refused it."""
    try:
        client.trigger_workflow(
            EVAL_CI_HEAL_WORKFLOW,
            ref=session.pr_ref,
            inputs={"scenarios": "", "credential": _DISPATCH_CREDENTIAL, "pr_ref": session.pr_ref},
        )
    except Exception:
        logger.exception("ci_eval_heal: dispatching the eval failed for %s", session.pr_ref)
        return False
    return True


def _halt_dispatch(session: "CiEvalHealSession", *, escalate: EscalateFn) -> str:
    """HALT a claimed step whose dispatch never landed — the run it awaits can never appear."""
    return _halt(session, escalate=escalate, reason="dispatching the eval failed; there is no CI run to await")


def _dispatch_ci(session: "CiEvalHealSession", *, client: GhCiEvalClient, escalate: EscalateFn) -> AdvanceOutcome:
    """PENDING → AWAITING_CI: claim the step, THEN dispatch the eval run it now owns.

    Claiming first is what makes the dispatch exactly-once, and is safe only because
    ``halt`` accepts ``AWAITING_CI`` as a source — unlike ``record_fix``'s ``PUSHED``
    target, which is why that one still records only after its push.
    """
    head_sha = client.resolve_head_sha(session.pr_ref)
    claimed = _advance_owned(session, lambda owned: owned.trigger(ci_run_id="", head_sha=head_sha))
    if claimed is None:
        return AdvanceOutcome(session.pr_ref, "pending", session.state, note=_SUPERSEDED_NOTE)
    if not _fire_workflow(claimed, client=client):
        return AdvanceOutcome(session.pr_ref, "pending", _halt_dispatch(claimed, escalate=escalate))
    return AdvanceOutcome(session.pr_ref, "pending", claimed.state, note=f"dispatched @ {head_sha[:12]}")


def _resolve_triage(
    session: "CiEvalHealSession", *, client: GhCiEvalClient, escalate: EscalateFn, fixer: CiEvalHealFixer
) -> str:
    """TRIAGING terminal: GREEN iff no red remains; a red HALTs or dispatches a bounded fix.

    ``mark_green`` is never reached while ``red_scenarios`` is non-empty (and the
    model's ``_no_reds`` guard would refuse it anyway). With a red: observe-only
    (:func:`~teatree.loop.ci_eval_heal_fixer.autofix_armed` false) HALTs + escalates;
    armed-but-budget-exhausted HALTs + escalates; armed-with-budget dispatches ONE
    bounded, anti-cheat-gated fix. A red NEVER self-certifies green.
    """
    if not session.red_scenarios:
        session.mark_green()
        session.save()
        return session.state
    if not autofix_armed(session):
        return _halt_red(session, escalate=escalate, detail="autofix disarmed (observe-only)")
    if session.fix_budget_exhausted:
        return _halt_red(
            session, escalate=escalate, detail=f"fix budget exhausted after {session.fix_attempts} attempt(s)"
        )
    return _dispatch_fix(session, client=client, escalate=escalate, fixer=fixer)


def _halt(session: "CiEvalHealSession", *, escalate: EscalateFn, reason: str) -> str:
    """HALT + escalate under the ownership CAS, so a rival advancer never pages the human twice."""
    halted = _advance_owned(session, lambda owned: owned.halt(reason=reason))
    if halted is None:
        return session.state
    escalate(halted)
    return halted.state


def _halt_red(session: "CiEvalHealSession", *, escalate: EscalateFn, detail: str) -> str:
    """HALT + escalate a session whose behavioral red is unresolved — never a false green."""
    reds = ", ".join(session.red_scenarios)
    return _halt(session, escalate=escalate, reason=f"behavioral eval red(s) unresolved — {detail}: {reds}")


def _dispatch_fix(
    session: "CiEvalHealSession", *, client: GhCiEvalClient, escalate: EscalateFn, fixer: CiEvalHealFixer
) -> str:
    """Dispatch ONE bounded autonomous fix — gate BEFORE publish, HALT on any refusal.

    ``begin_fix`` → the fixer PROPOSES a fix in a throwaway worktree (no push) → the
    #3282 anti-cheat gate runs over the proposed paths → on a clean gate the fix is
    PUBLISHED, only THEN recorded, and the eval re-triggered; a rejected (test-editing)
    or empty proposal, a failed publish, or any fixer failure is DISCARDED and the
    session HALTs + escalates — a red is never greened by editing its test, and the
    fixer never loops.

    ``record_fix`` lands the ``PUSHED`` transition AND consumes an attempt, and
    ``PUSHED`` is not a source state ``halt`` accepts — so recording it before the push
    landed stranded a failed publish as an un-haltable session claiming a fix that does
    not exist, whose only exit is re-triggering the eval on the unfixed branch until the
    budget burns out. The gate therefore runs on its own, ahead of the publish, and the
    transition follows the push it describes.
    """
    from teatree.core.gates.eval_heal_anticheat_gate import (  # noqa: PLC0415 — deferred: gate registered via the model
        EvalHealCheatError,
    )
    from teatree.core.modelkit.gate_registry import get_gate  # noqa: PLC0415 — deferred: gate registered via the model

    claimed = _advance_owned(session, lambda owned: owned.begin_fix())
    if claimed is None:
        return session.state
    try:
        proposal = fixer.propose(claimed)
    except Exception as exc:
        logger.exception("ci_eval_heal: fixer propose failed for %s", claimed.pr_ref)
        return _halt_red(claimed, escalate=escalate, detail=f"autonomous fixer dispatch failed: {type(exc).__name__}")
    if not proposal.changed_paths:
        fixer.discard(proposal)
        return _halt_red(
            claimed,
            escalate=escalate,
            detail="autonomous fixer produced no change (un-fixable without editing the test)",
        )
    changed_paths = list(proposal.changed_paths)
    try:
        get_gate("eval_heal_anticheat")(changed_paths)
    except EvalHealCheatError as exc:
        fixer.discard(proposal)
        return _halt_red(
            claimed, escalate=escalate, detail=f"autonomous fixer tried to edit the eval test — rejected ({exc})"
        )
    try:
        head_sha = fixer.publish(claimed, proposal)
    except Exception as exc:
        logger.exception("ci_eval_heal: publishing the fix failed for %s", claimed.pr_ref)
        fixer.discard(proposal)
        return _halt_red(claimed, escalate=escalate, detail=f"publishing the fix failed: {type(exc).__name__}")
    claimed.record_fix(changed_paths=changed_paths)
    claimed.save()
    return _retrigger(claimed, client=client, escalate=escalate, head_sha=head_sha)


def _retrigger(session: "CiEvalHealSession", *, client: GhCiEvalClient, escalate: EscalateFn, head_sha: str) -> str:
    """PUSHED → AWAITING_CI: claim the back-edge, then re-dispatch the eval on the fixed branch."""
    resolved = head_sha or client.resolve_head_sha(session.pr_ref)
    claimed = _advance_owned(session, lambda owned: owned.trigger(ci_run_id="", head_sha=resolved))
    if claimed is None:
        return session.state
    if not _fire_workflow(claimed, client=client):
        return _halt_dispatch(claimed, escalate=escalate)
    return claimed.state


def _triage_result(
    session: "CiEvalHealSession",
    *,
    reds: list[str],
    client: GhCiEvalClient,
    escalate: EscalateFn,
    fixer: CiEvalHealFixer,
) -> AdvanceOutcome:
    """Record the run's red set under the ownership CAS, then resolve the triage it opens."""
    triaging = _advance_owned(session, lambda owned: owned.receive_result(red_scenarios=reds))
    if triaging is None:
        return AdvanceOutcome(session.pr_ref, "awaiting_ci", session.state, note=_SUPERSEDED_NOTE)
    to_state = _resolve_triage(triaging, client=client, escalate=escalate, fixer=fixer)
    note = f"ci red: {len(reds)} scenario(s)" if reds else "ci green"
    return AdvanceOutcome(session.pr_ref, "awaiting_ci", to_state, note=note)


def _observe_ci(
    session: "CiEvalHealSession", *, client: GhCiEvalClient, escalate: EscalateFn, fixer: CiEvalHealFixer
) -> AdvanceOutcome:
    """AWAITING_CI: poll once; a finished run resolves to GREEN (success), a fix, or HALT (infra)."""
    runs = client.list_runs(EVAL_CI_HEAL_WORKFLOW, branch=session.pr_ref)
    run = _match_run(runs, head_sha=session.head_sha)
    if run is None or str(run.get("status") or "") != "completed":
        return AdvanceOutcome(session.pr_ref, "awaiting_ci", session.state, note="run in flight")
    conclusion = str(run.get("conclusion") or "")
    run_id = run.get("databaseId")
    if conclusion == "success":
        return _triage_result(session, reds=[], client=client, escalate=escalate, fixer=fixer)
    reds = _download_reds(client, run_id=int(run_id) if isinstance(run_id, int) else None, head_sha=session.head_sha)
    if reds:
        return _triage_result(session, reds=reds, client=client, escalate=escalate, fixer=fixer)
    # Non-success with NO confirmable behavioral red — an infra failure (transport,
    # throttle, cap, cancelled, or an unfetchable artifact). Never greened.
    reason = f"CI run concluded {conclusion or 'unknown'!r} with no confirmable behavioral red (infra)"
    to_state = _halt(session, escalate=escalate, reason=reason)
    return AdvanceOutcome(session.pr_ref, "awaiting_ci", to_state, note="infra halt")


def advance_session(
    session: "CiEvalHealSession",
    *,
    client: GhCiEvalClient,
    escalate: EscalateFn,
    fixer: CiEvalHealFixer | None = None,
) -> AdvanceOutcome:
    """Advance one open session ONE FSM step. ``FIXING`` and terminal states are no-ops.

    ``FIXING`` is only ever transient WITHIN a ``TRIAGING`` dispatch (a fix proposes,
    gates, publishes, and re-triggers in one step) — a session resting in ``FIXING``
    means a prior step crashed mid-fix, so it is left for the operator rather than
    silently retried. ``PUSHED`` re-triggers the eval (recovers a fix that pushed but
    crashed before re-dispatch). ``GREEN`` / ``HALTED`` are terminal.
    """
    from teatree.core.models import CiEvalHealSession  # noqa: PLC0415 — deferred: ORM enum needs the app registry

    resolved_fixer = fixer if fixer is not None else default_fixer()
    state = session.state
    if state == CiEvalHealSession.State.PENDING:
        return _dispatch_ci(session, client=client, escalate=escalate)
    if state == CiEvalHealSession.State.AWAITING_CI:
        return _observe_ci(session, client=client, escalate=escalate, fixer=resolved_fixer)
    if state == CiEvalHealSession.State.TRIAGING:
        to_state = _resolve_triage(session, client=client, escalate=escalate, fixer=resolved_fixer)
        return AdvanceOutcome(session.pr_ref, "triaging", to_state)
    if state == CiEvalHealSession.State.PUSHED:
        to_state = _retrigger(session, client=client, escalate=escalate, head_sha="")
        return AdvanceOutcome(session.pr_ref, "pushed", to_state, note="re-triggered eval after fix")
    return AdvanceOutcome(session.pr_ref, state, state, note="no-op (terminal or in-flight fix)")


def _escalate_via_deferred_question(session: "CiEvalHealSession") -> None:
    """Record a durable, deduped escalation for a HALTED session (the human surface)."""
    from teatree.core.models import DeferredQuestion  # noqa: PLC0415 — deferred: ORM needs the app registry

    marker = _HALT_MARKER.format(pk=session.pk)
    if DeferredQuestion.objects.filter(question__contains=marker).exists():
        return
    question = (
        f"{marker} CI-eval heal session for PR {session.pr_ref!r} (overlay {session.overlay!r}) HALTED and needs a "
        f"human: {session.halt_reason} The observe loop never edits a test or self-certifies a red — decide whether "
        "to fix the product behaviour the scenario asserts, re-open the session, or close it."
    )
    DeferredQuestion.record(question, session_id="")


@dataclass(slots=True)
class OpenSessionsRun:
    """Bookkeeping for one advance pass over every open session — outcomes + swallowed errors."""

    outcomes: list[AdvanceOutcome] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def advance_open_sessions(
    *,
    client: GhCiEvalClient | None = None,
    escalate: EscalateFn | None = None,
    fixer: CiEvalHealFixer | None = None,
) -> OpenSessionsRun:
    """Advance every non-terminal session one step, best-effort (a bad session never aborts the pass).

    Loads the open sessions (anything not GREEN / HALTED) and advances each. A
    per-session exception (a ``gh`` stall, a rolled-back transition) is logged and
    recorded, never raised — the next tick retries the un-advanced session. Returns
    the outcomes + swallowed errors for the caller (loop log / operator CLI). The
    ``fixer`` is the injected autonomous-fix seam (default: the production headless
    fixer); it only fires when :func:`~teatree.loop.ci_eval_heal_fixer.autofix_armed`.
    """
    from teatree.core.models import CiEvalHealSession  # noqa: PLC0415 — deferred: ORM needs the app registry

    resolved_client = client if client is not None else build_ci_eval_client(DEFAULT_CI_EVAL_REPO)
    resolved_escalate = escalate if escalate is not None else _escalate_via_deferred_question
    resolved_fixer = fixer if fixer is not None else default_fixer()
    run = OpenSessionsRun()
    terminal = (CiEvalHealSession.State.GREEN, CiEvalHealSession.State.HALTED)
    for session in CiEvalHealSession.objects.exclude(state__in=terminal).order_by("pk"):
        try:
            run.outcomes.append(
                advance_session(session, client=resolved_client, escalate=resolved_escalate, fixer=resolved_fixer)
            )
        except Exception as exc:
            logger.exception("ci_eval_heal: advancing session %s (%s) failed", session.pk, session.pr_ref)
            run.errors[f"ci_eval_heal:{session.pk}"] = f"{type(exc).__name__}: {exc}"
    return run


__all__ = [
    "AdvanceOutcome",
    "EscalateFn",
    "OpenSessionsRun",
    "advance_open_sessions",
    "advance_session",
    "red_scenario_names",
]
