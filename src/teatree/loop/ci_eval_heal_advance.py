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
push, so a fix editing ``evals/scenarios/**`` or a red matcher is REJECTED and
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
    """
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list):
        return []
    names: list[str] = []
    for raw in scenarios:
        if not isinstance(raw, dict):
            continue
        record = cast("RawAPIDict", raw)
        if record.get("triage_class") is None:
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


def _dispatch_ci(session: "CiEvalHealSession", *, client: GhCiEvalClient) -> AdvanceOutcome:
    """PENDING → AWAITING_CI: dispatch the full-suite eval and record the head SHA it keys on."""
    head_sha = client.resolve_head_sha(session.pr_ref)
    client.trigger_workflow(
        EVAL_CI_HEAL_WORKFLOW,
        ref=session.pr_ref,
        inputs={"scenarios": "", "credential": _DISPATCH_CREDENTIAL, "pr_ref": session.pr_ref},
    )
    session.trigger(ci_run_id="", head_sha=head_sha)
    session.save()
    return AdvanceOutcome(session.pr_ref, "pending", session.state, note=f"dispatched @ {head_sha[:12]}")


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


def _halt_red(session: "CiEvalHealSession", *, escalate: EscalateFn, detail: str) -> str:
    """HALT + escalate a session whose behavioral red is unresolved — never a false green."""
    reds = ", ".join(session.red_scenarios)
    session.halt(reason=f"behavioral eval red(s) unresolved — {detail}: {reds}")
    session.save()
    escalate(session)
    return session.state


def _dispatch_fix(
    session: "CiEvalHealSession", *, client: GhCiEvalClient, escalate: EscalateFn, fixer: CiEvalHealFixer
) -> str:
    """Dispatch ONE bounded autonomous fix — gate BEFORE publish, HALT on any refusal.

    ``begin_fix`` → the fixer PROPOSES a fix in a throwaway worktree (no push) → the
    #3282 anti-cheat gate (``record_fix``) runs over the proposed paths → on a clean
    gate the fix is PUBLISHED and the eval re-triggered; a rejected (test-editing) or
    empty proposal, or any fixer failure, is DISCARDED and the session HALTs +
    escalates — a red is never greened by editing its test, and the fixer never loops.
    """
    from teatree.core.gates.eval_heal_anticheat_gate import (  # noqa: PLC0415 — deferred: gate registered via the model
        EvalHealCheatError,
    )

    session.begin_fix()
    session.save()
    try:
        proposal = fixer.propose(session)
    except Exception as exc:
        logger.exception("ci_eval_heal: fixer propose failed for %s", session.pr_ref)
        return _halt_red(session, escalate=escalate, detail=f"autonomous fixer dispatch failed: {type(exc).__name__}")
    if not proposal.changed_paths:
        fixer.discard(proposal)
        return _halt_red(
            session,
            escalate=escalate,
            detail="autonomous fixer produced no change (un-fixable without editing the test)",
        )
    try:
        session.record_fix(changed_paths=list(proposal.changed_paths))
    except EvalHealCheatError as exc:
        fixer.discard(proposal)
        return _halt_red(
            session, escalate=escalate, detail=f"autonomous fixer tried to edit the eval test — rejected ({exc})"
        )
    session.save()
    head_sha = fixer.publish(session, proposal)
    return _retrigger(session, client=client, head_sha=head_sha)


def _retrigger(session: "CiEvalHealSession", *, client: GhCiEvalClient, head_sha: str) -> str:
    """PUSHED → AWAITING_CI: re-dispatch the eval on the fixed branch (the loop back-edge)."""
    resolved = head_sha or client.resolve_head_sha(session.pr_ref)
    client.trigger_workflow(
        EVAL_CI_HEAL_WORKFLOW,
        ref=session.pr_ref,
        inputs={"scenarios": "", "credential": _DISPATCH_CREDENTIAL, "pr_ref": session.pr_ref},
    )
    session.trigger(ci_run_id="", head_sha=resolved)
    session.save()
    return session.state


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
        session.receive_result(red_scenarios=[])
        session.save()
        to_state = _resolve_triage(session, client=client, escalate=escalate, fixer=fixer)
        return AdvanceOutcome(session.pr_ref, "awaiting_ci", to_state, note="ci green")
    reds = _download_reds(client, run_id=int(run_id) if isinstance(run_id, int) else None, head_sha=session.head_sha)
    if reds:
        session.receive_result(red_scenarios=reds)
        session.save()
        to_state = _resolve_triage(session, client=client, escalate=escalate, fixer=fixer)
        return AdvanceOutcome(session.pr_ref, "awaiting_ci", to_state, note=f"ci red: {len(reds)} scenario(s)")
    # Non-success with NO confirmable behavioral red — an infra failure (transport,
    # throttle, cap, cancelled, or an unfetchable artifact). Never greened.
    session.halt(reason=f"CI run concluded {conclusion or 'unknown'!r} with no confirmable behavioral red (infra)")
    session.save()
    escalate(session)
    return AdvanceOutcome(session.pr_ref, "awaiting_ci", session.state, note="infra halt")


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
        return _dispatch_ci(session, client=client)
    if state == CiEvalHealSession.State.AWAITING_CI:
        return _observe_ci(session, client=client, escalate=escalate, fixer=resolved_fixer)
    if state == CiEvalHealSession.State.TRIAGING:
        to_state = _resolve_triage(session, client=client, escalate=escalate, fixer=resolved_fixer)
        return AdvanceOutcome(session.pr_ref, "triaging", to_state)
    if state == CiEvalHealSession.State.PUSHED:
        to_state = _retrigger(session, client=client, head_sha="")
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
