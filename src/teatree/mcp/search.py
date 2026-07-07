"""Read-only structured-search queries over the teatree core models.

These are the synchronous query functions the MCP tools expose. Each reuses the
existing model managers (``Ticket.objects.for_overlay`` / ``in_flight`` /
``resolve``, ``Worktree.objects.active``, ``Task.objects.for_overlay``,
``IncomingEvent.objects.unprocessed``) rather than re-deriving query logic in
the protocol layer — the manager is the
single source of truth for what "in-flight" or "active" or "this overlay" means,
and the MCP surface is a thin read over it.

Every function returns already-serialized JSON-safe data so the async server
wrappers only have to cross the sync boundary once (see
:mod:`teatree.mcp.server`). They never mutate — write operations stay on the
FSM-guarded ``t3`` CLI per BLUEPRINT's orchestrator-decides / loop-executes
topology.
"""

from typing import Any

from django.db.models import Count, Q

from teatree.config import COLD_HOOK_SETTINGS, OVERLAY_OVERRIDABLE_SETTINGS, cold_reader, get_effective_settings
from teatree.config.registries import REGISTRY_SETTINGS
from teatree.core.factory.factory_score import score as factory_score_compute
from teatree.core.factory.factory_signals import DEFAULT_WINDOW_DAYS, compute_factory_signals
from teatree.core.modelkit.phases import phase_spellings
from teatree.core.models import ConfigSetting, IncomingEvent, PullRequest, ReplyDispatch, Task, Ticket, Worktree
from teatree.mcp import command_catalogue
from teatree.mcp.serializers import (
    serialize_incoming_event,
    serialize_pull_request,
    serialize_task,
    serialize_ticket,
    serialize_ticket_detail,
    serialize_worktree,
)

_DEFAULT_TICKET_LIMIT = 50
_DEFAULT_EVENT_LIMIT = 20
_DEFAULT_TASK_LIMIT = 50
_DEFAULT_COMMAND_LIMIT = 20
_MAX_LIMIT = 200

# The review/merge-governing settings agents most need to read before deciding
# whether a merge is theirs to make — the review gate proper plus the review-phase
# evidence gates. All are ``UserSettings`` bool fields resolved via the effective
# settings, so a per-overlay override is reflected.
_REVIEW_GATE_KEYS = (
    "require_human_approval_to_merge",
    "require_reviewed_state_for_review_request",
    "require_review_context",
    "require_anti_vacuity_attestation",
    "require_merge_evidence",
    "e2e_mandatory_gate_enabled",
)
# The raw/out-of-band merge gate is a cold-hook key (no ``UserSettings`` field),
# resolved from the canonical config DB with its registered fail-open default.
_RAW_MERGE_GATE_KEY = "out_of_band_merge_gate_enabled"


def _capped(limit: int, default: int) -> int:
    """A bounded row cap for a page request.

    A non-positive request falls back to *default*, and any request is clamped
    to ``_MAX_LIMIT`` so a client cannot ask the read-only server for an
    unbounded page.
    """
    return min(limit if limit and limit > 0 else default, _MAX_LIMIT)


# ast-grep-ignore: ac-django-no-complexity-suppressions
def ticket_search(  # noqa: PLR0913 — search entry-point; each kwarg is a documented, keyword-only filter, mapped 1:1 to the public MCP tool input schema, not an internal design smell.
    *,
    overlay: str | None = None,
    state: str | None = None,
    kind: str | None = None,
    role: str | None = None,
    text: str | None = None,
    in_flight: bool = False,
    limit: int = _DEFAULT_TICKET_LIMIT,
) -> list[dict[str, Any]]:
    """Tickets matching the given filters, newest first.

    ``overlay`` scopes through ``TicketManager.for_overlay`` (which also
    includes legacy empty-overlay rows). ``in_flight=True`` narrows to the
    not-yet-delivered/ignored set via ``TicketManager.in_flight``. ``text`` is a
    case-insensitive substring match across ``issue_url``, ``short_description``
    and the durable per-ticket ``context``.
    """
    queryset = Ticket.objects.in_flight(overlay) if in_flight else Ticket.objects.for_overlay(overlay)
    if state:
        queryset = queryset.filter(state=state)
    if kind:
        queryset = queryset.filter(kind=kind)
    if role:
        queryset = queryset.filter(role=role)
    if text:
        queryset = queryset.filter(
            Q(issue_url__icontains=text) | Q(short_description__icontains=text) | Q(context__icontains=text),
        )
    rows = queryset.order_by("-pk")[: _capped(limit, _DEFAULT_TICKET_LIMIT)]
    return [serialize_ticket(ticket) for ticket in rows]


def ticket_list(
    *,
    overlay: str | None = None,
    state: str | None = None,
    in_flight: bool = False,
    limit: int = _DEFAULT_TICKET_LIMIT,
) -> list[dict[str, Any]]:
    """The MCP mirror of ``t3 <overlay> ticket list`` — enumerate tickets by state.

    The list counterpart to the free-text ``ticket_search``: filter by lifecycle
    ``state`` (the stage an agent means by "phase"), scope to an ``overlay``, and
    set ``in_flight=True`` for the not-yet-delivered/ignored set. Delegates to
    ``ticket_search`` (kind / role / free-text refinements live there). For one
    ticket's full detail (including its visited-phase ledger) use ``ticket_get``.
    """
    return ticket_search(overlay=overlay, state=state, in_flight=in_flight, limit=limit)


def ticket_get(*, ticket: str) -> dict[str, Any]:
    """One ticket's full detail, resolved by pk, issue number, URL, or repo key.

    Resolves through ``TicketManager.resolve`` and returns the base ticket fields
    plus ``visited_phases`` (the union of lifecycle phases recorded across the
    ticket's sessions). An unresolvable reference returns an empty dict rather
    than raising, so the long-running server never crashes on a bad argument.
    """
    try:
        resolved = Ticket.objects.resolve(ticket)
    except Ticket.DoesNotExist:
        return {}
    return serialize_ticket_detail(resolved)


def worktree_status(
    *,
    ticket: str | None = None,
    overlay: str | None = None,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    """Worktrees for a ticket, or the overlay's worktrees when no ticket is given.

    ``ticket`` is resolved through the canonical ``TicketManager.resolve`` (pk,
    bare issue number, or full issue URL); an unresolvable reference returns an
    empty list rather than raising, so the long-running server never crashes on
    a bad argument. Without a ticket, ``active_only`` (default) lists the
    overlay's in-flight worktrees via ``WorktreeManager.active``.
    """
    if ticket:
        try:
            resolved = Ticket.objects.resolve(ticket)
        except Ticket.DoesNotExist:
            return []
        queryset = Worktree.objects.filter(ticket=resolved)
    elif active_only:
        queryset = Worktree.objects.active(overlay)
    else:
        queryset = Worktree.objects.for_overlay(overlay)
    rows = queryset.select_related("ticket").order_by("-pk")
    return [serialize_worktree(worktree) for worktree in rows]


def pr_for_ticket(*, ticket: str) -> list[dict[str, Any]]:
    """Open/merged pull requests recorded for a ticket (newest first).

    A ticket can carry several PRs (one per repo in a multi-repo ticket), so
    this returns a list. An unresolvable ``ticket`` reference returns an empty
    list.
    """
    try:
        resolved = Ticket.objects.resolve(ticket)
    except Ticket.DoesNotExist:
        return []
    rows = PullRequest.objects.filter(ticket=resolved).order_by("-pk")
    return [serialize_pull_request(pull_request) for pull_request in rows]


def loop_stats(*, overlay: str | None = None) -> dict[str, Any]:
    """Task-status counts for the autonomous loop, plus the dead-letter total.

    ``tasks`` is a count per ``Task.Status`` (pending / claimed / completed /
    failed) — the loop's work queue at a glance. ``overlay`` scopes the tasks
    through ``TaskQuerySet.for_overlay`` — the same overlay clause the loop's
    own claim path uses (spanning the ticket and session relations, legacy
    empty-overlay rows included). ``dead_letter`` is the global count of reply
    dispatches that exhausted their retries — a system-health signal that is
    not overlay-scoped (the inbound event it answers carries no overlay).
    """
    tasks = Task.objects.for_overlay(overlay)
    counts = {status.value: 0 for status in Task.Status}
    for row in tasks.values("status").annotate(total=Count("pk")):
        counts[row["status"]] = row["total"]
    dead_letter = ReplyDispatch.objects.filter(status=ReplyDispatch.Status.DEAD_LETTER).count()
    return {"overlay": overlay or "", "tasks": counts, "dead_letter": dead_letter}


def factory_signals(*, overlay: str | None = None, window_days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """The derived-on-read factory quality/velocity signals report (read-only).

    Delegates to :func:`teatree.core.factory.factory_signals.compute_factory_signals` —
    the same computation path ``t3 <overlay> signals`` uses. Returns the report's
    ``to_dict()``: the ``overlay`` scope, the trailing window's five signals
    (first-try-green, defect-escape, review-catch, merge-latency, repair-burn)
    with their fail-loud statuses, red-floor trips, and the top-level verdict the
    outer loop keys on.

    Scope: an omitted ``overlay`` means the whole-factory GLOBAL view (``""``) —
    where the CLI reads ``T3_OVERLAY_NAME`` and scopes to the active overlay,
    this MCP tool defaults global unless the caller passes one. Both surfaces now
    stamp the resolved scope in the payload's ``overlay`` field so a global
    reading is distinguishable from a scoped one from the output alone (#25). The
    two surfaces are held schema-identical by the named
    ``tests/conformance/test_signals_scope_parity.py`` lane — the parity guard
    that replaced the earlier unenforced "can never drift" docstring claim.
    """
    report = compute_factory_signals(window_days=window_days, overlay=overlay or "")
    return report.to_dict()


def factory_score(*, overlay: str | None = None, window_days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """The recipe-weighted factory score over the trailing window (read-only).

    Delegates to :func:`teatree.core.factory.factory_score.score` — the same recipe fold
    ``t3 <overlay> recipe score`` uses. Returns the score payload: the aggregate
    (``None`` when untrustworthy), the ``ok`` / ``regressing`` / ``red`` verdict,
    coverage vs floor, the recipe provenance (``recipe_sha`` + ``recipe_approved``),
    the snapshot deltas, and the per-signal contributions. Registered only when
    ``factory_score_enabled`` is on — absent otherwise (the shipped OFF state).
    """
    settings = get_effective_settings(overlay or None)
    result = factory_score_compute(
        window_days=window_days,
        overlay=overlay or "",
        approved_recipe_sha=settings.approved_recipe_sha,
    )
    return result.to_dict()


def incoming_event_recent(
    *,
    limit: int = _DEFAULT_EVENT_LIMIT,
    source: str | None = None,
    unprocessed_only: bool = False,
) -> list[dict[str, Any]]:
    """The most recent inbound platform events, newest first.

    ``source`` filters to one platform (``slack`` / ``gitlab`` / ``github`` /
    ``notion`` / ``ci``). ``unprocessed_only`` narrows to events the dispatcher
    has not yet handled via ``IncomingEventManager.unprocessed``.
    """
    queryset = IncomingEvent.objects.unprocessed() if unprocessed_only else IncomingEvent.objects.all()
    if source:
        queryset = queryset.filter(source=source)
    rows = queryset.order_by("-received_at", "-pk")[: _capped(limit, _DEFAULT_EVENT_LIMIT)]
    return [serialize_incoming_event(event) for event in rows]


def task_list(
    *,
    overlay: str | None = None,
    status: str | None = None,
    phase: str | None = None,
    ticket: str | None = None,
    limit: int = _DEFAULT_TASK_LIMIT,
) -> list[dict[str, Any]]:
    """The autonomous loop's task queue, newest first — the MCP mirror of ``tasks list``.

    ``status`` filters to one ``Task.Status`` (pending / claimed / completed /
    failed). ``phase`` matches any accepted spelling of a phase (short verb or
    gerund, via ``phase_spellings``). ``overlay`` scopes through
    ``TaskManager.for_overlay`` (spanning the ticket and session relations,
    legacy empty-overlay rows included). ``ticket`` narrows to one ticket
    (resolved through ``TicketManager.resolve``); an unresolvable reference
    returns an empty list rather than raising.
    """
    queryset = Task.objects.for_overlay(overlay)
    if ticket:
        try:
            resolved = Ticket.objects.resolve(ticket)
        except Ticket.DoesNotExist:
            return []
        queryset = queryset.filter(ticket=resolved)
    if status:
        queryset = queryset.filter(status=status)
    if phase:
        queryset = queryset.filter(phase__in=phase_spellings(phase))
    rows = queryset.select_related("ticket").order_by("-pk")[: _capped(limit, _DEFAULT_TASK_LIMIT)]
    return [serialize_task(task) for task in rows]


def _scope_label(scope: str) -> str:
    """``global`` for the empty scope, else ``overlay:<name>`` — mirrors the CLI label."""
    return "global" if not scope else f"overlay:{scope}"


def _jsonable(value: object) -> object:
    """Coerce a resolved config value to a JSON-safe primitive for the boundary.

    A ``ConfigSetting`` row is already JSON; a ``UserSettings`` fallback may be a
    ``StrEnum``, ``Path`` or other rich type, so anything not a plain primitive
    is stringified so the read-only tool never fails to serialize.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


def config_setting_get(*, key: str, overlay: str | None = None) -> dict[str, Any]:
    """The effective value of a config setting and where it resolves from.

    The read side of the DB override store, mirroring ``t3 <overlay>
    config_setting get``: a ``ConfigSetting`` row in the requested scope is
    reported as ``source == "db"``; otherwise the value falls through to the
    file/env layer (``source == "file/env"``). ``overlay`` reads that overlay's
    scope. A key in neither the overridable nor the registry partition is
    reported ``known == False`` (value ``None``) rather than raising — the read
    surface stays crash-proof on a typo.
    """
    scope = overlay or ""
    label = _scope_label(scope)
    if key not in OVERLAY_OVERRIDABLE_SETTINGS and key not in REGISTRY_SETTINGS:
        return {"key": key, "known": False, "value": None, "source": None, "scope": label, "overlay": scope}
    stored = ConfigSetting.objects.get_effective(key, scope=scope)
    if stored is not None:
        return {"key": key, "known": True, "value": _jsonable(stored), "source": "db", "scope": label, "overlay": scope}
    fallback = getattr(get_effective_settings(overlay or None), key, None)
    return {
        "key": key,
        "known": True,
        "value": _jsonable(fallback),
        "source": "file/env",
        "scope": label,
        "overlay": scope,
    }


def gate_status(*, overlay: str | None = None) -> dict[str, Any]:
    """The review-gate and raw-merge gate state — the merge-governing gates at a glance.

    ``review_gate`` reports whether a human must approve a merge
    (``require_human_approval_to_merge``) plus the review-phase evidence gates,
    all resolved through the effective settings so a per-``overlay`` override is
    reflected. ``raw_merge_gate`` reports whether raw ``gh``/``glab`` merges are
    blocked (``out_of_band_merge_gate_enabled``), resolved from the canonical
    config DB with its registered fail-open default. Read-only: flip a gate with
    ``t3 <overlay> config_setting set`` / ``t3 <overlay> gate``.
    """
    settings = get_effective_settings(overlay or None)
    review_gate = {key: bool(getattr(settings, key)) for key in _REVIEW_GATE_KEYS}
    raw_merge_gate = {
        _RAW_MERGE_GATE_KEY: cold_reader.bool_setting(
            _RAW_MERGE_GATE_KEY,
            default=bool(COLD_HOOK_SETTINGS[_RAW_MERGE_GATE_KEY].default),
        ),
    }
    return {"overlay": overlay or "", "review_gate": review_gate, "raw_merge_gate": raw_merge_gate}


def command_search(*, query: str, limit: int = _DEFAULT_COMMAND_LIMIT) -> list[dict[str, Any]]:
    """The `t3` leaf commands matching *query* — the CLI-discoverability read.

    Answers "which `t3` command do I run for X" so an agent stops guessing
    subcommands that do not exist. Each match carries the full invocation
    ``path``, its one-line help ``summary``, and ``emits_json`` (whether it
    exposes a ``--json`` / ``--format`` output the agent can parse). Sourced from
    the live Typer command tree via the registered catalogue provider, best match
    first.
    """
    return command_catalogue.search_commands(
        query,
        catalogue=command_catalogue.build_command_catalogue(),
        limit=_capped(limit, _DEFAULT_COMMAND_LIMIT),
    )
