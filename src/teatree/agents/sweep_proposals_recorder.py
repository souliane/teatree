"""Validate a read-only sweep's proposals and let the worker record only gated actions."""

import io
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from html import escape
from typing import Never

from teatree.agents.read_only_venue_brief import owned_issue_url, sweep_window_evidence
from teatree.core.backend_registry import get_backend_provider
from teatree.core.machine_output import call_command_streamed
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.phases import SWEEP_ASK_GATE_MARKER, normalize_phase
from teatree.core.modelkit.question_card import CardOption, QuestionCard, internal_shorthand
from teatree.core.modelkit.task_failure_taxonomy import SWEEP_PROPOSALS_REFUSAL_PREFIX
from teatree.core.models import DeferredQuestion, Task, Ticket, TicketSweepRun
from teatree.core.models.dream_gap_ledger import batch_entries, dream_umbrella_url, pending_entries, umbrella_ticket
from teatree.core.models.dream_gap_offer import host_gap_capacity_refusal
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_overlay_for_ticket
from teatree.core.overlay_repos import owned_repo_slugs
from teatree.core.self_forge_identities import require_self_authored_issue
from teatree.dream_constants import DREAM_BATCH_MANIFEST_HEADER

REFUSAL_PREFIX = SWEEP_PROPOSALS_REFUSAL_PREFIX
QUESTION_CITATION_CHARS = 240
logger = logging.getLogger(__name__)


class SweepProposalError(ValueError):
    pass


@dataclass(slots=True)
class _ValidationContext:
    overlay: OverlayBase
    repos: set[str]
    index_urls: list[str]
    verified: set[str] = field(default_factory=set)


def _invalid(reason: str) -> Never:
    raise SweepProposalError(reason)


def record_returned_sweep_proposals(task: Task, result: Mapping[str, object], *, phase: str) -> str:
    if (
        normalize_phase(phase or task.phase) != "backlog_sweep"
        or result.get("needs_user_input")
        or "sweep_proposals" not in result
    ):
        return ""
    try:
        if SWEEP_ASK_GATE_MARKER not in task.execution_reason:
            _invalid("the sweep lacks the ASK-GATE marker")
        _resume_pending_run(task)
        task.ticket.refresh_from_db(fields=["extra"])
        extra = task.ticket.extra if isinstance(task.ticket.extra, dict) else {}
        recorded = extra.get("sweep_fold_proposals")
        if isinstance(recorded, dict) and str(task.pk) in recorded:
            return ""
        validated = _validate(task, result["sweep_proposals"])
        examined, external_count, folds, gaps = validated
        run = TicketSweepRun.objects.begin(source="loop", overlay=task.ticket.overlay)
        task.ticket.merge_extra(
            set_keys={
                "sweep_pending_run": {
                    "task_id": task.pk,
                    "run_id": run.run_id,
                    "examined_count": len(examined),
                    "external_count": external_count,
                    "folds": folds,
                    "gaps": gaps,
                }
            }
        )
        _resume_pending_run(task)
    except SweepProposalError as exc:
        return f"{REFUSAL_PREFIX}{exc}"
    return ""


def _resume_pending_run(task: Task) -> None:
    task.ticket.refresh_from_db(fields=["extra"])
    extra = task.ticket.extra if isinstance(task.ticket.extra, dict) else {}
    pending = extra.get("sweep_pending_run")
    if not isinstance(pending, dict):
        return
    try:
        prior_task = Task.objects.get(pk=pending["task_id"], ticket=task.ticket)
        run = TicketSweepRun.objects.get(run_id=pending["run_id"], overlay=task.ticket.overlay)
        folds = pending["folds"]
        gaps = pending["gaps"]
        if run.finished_at is None:
            _apply(prior_task, run.run_id, folds, gaps)
            TicketSweepRun.objects.finish(
                run_id=run.run_id,
                examined_count=pending["examined_count"],
                external_skipped_count=pending["external_count"],
            )
        task.ticket.merge_extra(
            merge_into_dicts={"sweep_fold_proposals": {str(prior_task.pk): {"folds": folds, "run_id": run.run_id}}},
            pop_keys=["sweep_pending_run"],
        )
    except (Exception, SystemExit) as exc:
        if pending.get("task_id") != task.pk:
            logger.warning("Abandoning failed pending sweep run %s: %s", pending.get("run_id"), exc)
            task.ticket.merge_extra(pop_keys=["sweep_pending_run"])
            return
        message = f"a pending sweep could not be finished ({type(exc).__name__}: {exc})"
        raise SweepProposalError(message) from exc


def _owner_citation(raw: str) -> str:
    citation = " ".join(raw.split())
    while tokens := internal_shorthand(citation):
        token = tokens[0]
        if re.fullmatch(r"(?:PR|pull request|MR|merge request)\s+[#!]?\d+", token, re.IGNORECASE):
            replacement = "pull request"
        elif token.startswith("tests/"):
            replacement = "test file"
        elif token.startswith("src/") or token.endswith((".py", ".md", ".toml")):
            replacement = "source file"
        elif token.startswith("--"):
            replacement = "command option"
        else:
            replacement = "code reference"
        assignment = r"(?:=[^\s,;]+)?" if token.startswith("--") or "_" in token else ""
        updated = re.sub(rf"(?<!\w){re.escape(token)}{assignment}(?!\w)", replacement, citation, count=1)
        if updated == citation:
            citation = "The detailed citation is retained in the recorded proposal."
            break
        citation = updated
    safe = escape(citation, quote=False)
    if len(safe) > QUESTION_CITATION_CHARS:
        prefix = re.sub(r"&[^;\s]*$", "", safe[: QUESTION_CITATION_CHARS - 3])
        safe = f"{prefix.rsplit(' ', 1)[0].rstrip()}..."
    return safe


def _apply(task: Task, run_id: str, folds: list[dict[str, str]], gaps: list[tuple[int, list[str]]]) -> None:
    try:
        for position, fold in enumerate(folds):
            citation = _owner_citation(fold["citation"])
            DeferredQuestion.record(
                "Should this proposed ticket fold be approved?",
                task_session=task.session,
                dedupe_marker=f"backlog-sweep-fold-{task.pk}-{position}",
                card=QuestionCard(
                    decision=OwnerDecision.ARCHITECTURE,
                    checked=("Both issues were open and self-authored when the proposal was recorded.",),
                    blocker="Only the owner decides whether this ticket fold should proceed.",
                    why="Your decision is recorded; this sweep changes neither issue.",
                    quoted=f"Fold <{fold['member_url']}|member issue> into <{fold['host_url']}|host issue>: {citation}",
                    options=(
                        CardOption("Approve", "Record approval of this proposal.", recommended=True),
                        CardOption("Decline", "Record that this proposal is declined."),
                    ),
                ),
            )
        for host_id, keys in gaps:
            host_ticket = Ticket.objects.get(pk=host_id)
            host_ticket.refresh_from_db(fields=["extra"])
            attached = {entry.get("gap_key") for entry in batch_entries(host_ticket)}
            remaining = [key for key in keys if key not in attached]
            if not remaining:
                continue
            output = io.StringIO()
            try:
                call_command_streamed(
                    "ticket",
                    "attach-gaps",
                    str(host_id),
                    "--manifest",
                    json.dumps(remaining),
                    "--sweep-run-id",
                    run_id,
                    stream=output,
                )
            except (Exception, SystemExit) as exc:
                raise SweepProposalError(output.getvalue().strip() or str(exc)) from exc
    except SweepProposalError:
        raise
    except (Exception, SystemExit) as exc:
        message = f"the worker action failed ({type(exc).__name__}: {exc})"
        raise SweepProposalError(message) from exc


def _validate(task: Task, raw: object) -> tuple[list[str], int, list[dict[str, str]], list[tuple[int, list[str]]]]:
    if not isinstance(raw, Mapping):
        _invalid("expected an object")
    examined_raw, folds_raw, gaps_raw = (raw.get(key) for key in ("examined", "folds", "gaps"))
    if not isinstance(examined_raw, list) or not isinstance(folds_raw, list) or not isinstance(gaps_raw, list):
        _invalid("examined, folds and gaps must be arrays")
    try:
        overlay = get_overlay_for_ticket(task.ticket)
        repos = {slug.casefold() for slug in owned_repo_slugs(overlay)}
    except Exception as exc:
        message = f"the overlay could not be read ({type(exc).__name__})"
        raise SweepProposalError(message) from exc
    if not repos:
        _invalid("the overlay has no owned issue repositories")
    examined = _validated_examined(examined_raw, repos)
    try:
        index_urls, expected, external_count = sweep_window_evidence(task)
    except Exception as exc:
        message = f"the dispatched issue window could not be read ({type(exc).__name__})"
        raise SweepProposalError(message) from exc
    if len(examined) != len(expected) or len(set(examined)) != len(examined) or set(examined) != set(expected):
        _invalid("examined does not match the dispatched issue window")
    context = _ValidationContext(overlay, repos, index_urls)
    folds = [_validate_fold(item, examined, context) for item in folds_raw]
    _validate_fold_graph(folds)
    gaps = [_validate_gap(item, task, context) for item in gaps_raw]
    assigned: set[str] = set()
    by_host: dict[int, list[str]] = {}
    for host_id, keys in gaps:
        if assigned.intersection(keys) or len(keys) != len(set(keys)):
            _invalid("a gap key is assigned more than once")
        assigned.update(keys)
        by_host.setdefault(host_id, []).extend(keys)
    for host_id, keys in by_host.items():
        _validate_gap_host(Ticket.objects.get(pk=host_id), keys)
    return examined, external_count, folds, gaps


def _validate_fold_graph(folds: list[dict[str, str]]) -> None:
    hosts_by_member: dict[str, str] = {}
    for fold in folds:
        member, host = fold["member_url"], fold["host_url"]
        if member in hosts_by_member:
            _invalid("a fold member appears more than once")
        hosts_by_member[member] = host
    for member in hosts_by_member:
        seen: set[str] = set()
        current = member
        while current in hosts_by_member:
            if current in seen:
                _invalid("folds form a cycle")
            seen.add(current)
            current = hosts_by_member[current]


def _validated_examined(raw: list[object], repos: set[str]) -> list[str]:
    if any(not owned_issue_url(url, repos) for url in raw):
        _invalid("examined contains an invalid or unowned issue URL")
    return [str(url) for url in raw]


def _validate_fold(raw: object, examined: list[str], context: _ValidationContext) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        _invalid("a fold is not an object")
    member, host, citation = (raw.get(key) for key in ("member_url", "host_url", "citation"))
    if not owned_issue_url(member, context.repos) or not owned_issue_url(host, context.repos):
        _invalid("a fold has an invalid member or host")
    if member == host or member not in examined or host not in context.index_urls:
        _invalid("a fold target is not a distinct indexed issue")
    if not isinstance(citation, str) or not citation.strip():
        _invalid("a fold has no citation")
    for url in (member, host):
        _verify_open_self_issue(context, url)
    return {"member_url": member, "host_url": host, "citation": citation.strip()}


def _validate_gap(raw: object, task: Task, context: _ValidationContext) -> tuple[int, list[str]]:
    if not isinstance(raw, Mapping):
        _invalid("a gap assignment is not an object")
    host_url, keys = raw.get("host_url"), raw.get("gap_keys")
    if not owned_issue_url(host_url, context.repos) or host_url not in context.index_urls:
        _invalid("a gap assignment has no valid host")
    if not isinstance(keys, list) or not keys or any(not isinstance(key, str) or not key.strip() for key in keys):
        _invalid("a gap assignment has no valid keys")
    if not set(keys) <= _offered_gap_keys(task.execution_reason):
        _invalid("a gap assignment names a key not offered in this sweep")
    host_ticket = Ticket.objects.filter(issue_url=host_url, overlay=task.ticket.overlay).first()
    if host_ticket is None:
        _invalid("a gap host is not an owned ticket")
    _verify_open_self_issue(context, host_url)
    return host_ticket.pk, keys


def _validate_gap_host(host: Ticket, keys: list[str]) -> None:
    host.refresh_from_db(fields=["state", "extra"])
    attached = {entry.get("gap_key") for entry in batch_entries(host)}
    remaining = set(keys) - attached
    if not remaining:
        return
    if host.is_settled:
        _invalid("a gap host is settled")
    umbrella = umbrella_ticket(dream_umbrella_url())
    if umbrella is None:
        _invalid("the offered gaps have no umbrella ticket")
    pending = {entry.get("gap_key"): entry for entry in pending_entries(umbrella)}
    if not remaining <= pending.keys():
        _invalid("a gap key is no longer pending on the umbrella")
    entries = [pending[key] for key in keys if key in remaining]
    if refusal := host_gap_capacity_refusal(host, entries):
        _invalid(refusal)


def _offered_gap_keys(reason: str) -> set[str]:
    _, marker, manifest = reason.partition(f"{DREAM_BATCH_MANIFEST_HEADER}\n")
    if not marker:
        return set()
    return {line[2:].partition(":")[0].strip() for line in manifest.splitlines() if line.startswith("- ")}


def _verify_open_self_issue(context: _ValidationContext, url: str) -> None:
    if url in context.verified:
        return
    try:
        code_host = get_backend_provider().get_code_host_for_url(context.overlay, url)
        if code_host is None:
            _invalid("a proposal target has no code host")
        issue = require_self_authored_issue(host=code_host, issue_url=url)
    except Exception as exc:
        message = f"a proposal target could not be verified ({exc})"
        raise SweepProposalError(message) from exc
    if issue.get("state") not in {"open", "opened"}:
        _invalid("a proposal target is not open")
    context.verified.add(url)
