"""Validate a read-only sweep's proposals and let the worker record only gated actions."""

import io
import json
import re
from collections.abc import Mapping
from html import escape
from typing import Never
from urllib.parse import urlparse

from teatree.agents.read_only_venue_brief import sweep_window_evidence
from teatree.core.backend_registry import get_backend_provider
from teatree.core.machine_output import call_command_streamed
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.phases import SWEEP_ASK_GATE_MARKER, normalize_phase
from teatree.core.modelkit.question_card import CardOption, QuestionCard, internal_shorthand
from teatree.core.models import DeferredQuestion, Task, Ticket, TicketSweepRun
from teatree.core.models.dream_gap_ledger import batch_entries
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_overlay_for_ticket
from teatree.core.overlay_repos import owned_repo_slugs
from teatree.core.self_forge_identities import require_self_authored_issue
from teatree.dream_constants import DREAM_BATCH_MANIFEST_HEADER
from teatree.utils.url_slug import repo_namespaced_key_from_path, slug_from_issue_or_pr_url

REFUSAL_PREFIX = "sweep_proposals_refused: "
QUESTION_CITATION_CHARS = 240


class SweepProposalError(ValueError):
    pass


def _invalid(reason: str) -> Never:
    raise SweepProposalError(reason)


def record_returned_sweep_proposals(
    task: Task, result: Mapping[str, object], *, phase: str, selected_harness: str
) -> str:
    if (
        normalize_phase(phase or task.phase) != "backlog_sweep"
        or selected_harness != "codex_app_server"
        or result.get("needs_user_input")
        or "sweep_proposals" not in result
    ):
        return ""
    if SWEEP_ASK_GATE_MARKER not in task.execution_reason:
        return f"{REFUSAL_PREFIX}the sweep lacks the ASK-GATE marker"
    task.ticket.refresh_from_db(fields=["extra"])
    extra = task.ticket.extra if isinstance(task.ticket.extra, dict) else {}
    recorded = extra.get("sweep_fold_proposals")
    if isinstance(recorded, dict) and str(task.pk) in recorded:
        return ""
    try:
        validated = _validate(task, result["sweep_proposals"])
    except SweepProposalError as exc:
        return f"{REFUSAL_PREFIX}{exc}"
    examined, external_count, folds, gaps = validated
    run = TicketSweepRun.objects.begin(source="loop", overlay=task.ticket.overlay)
    try:
        _apply(task, run.run_id, folds, gaps)
    except SweepProposalError as exc:
        return f"{REFUSAL_PREFIX}{exc}"
    TicketSweepRun.objects.finish(
        run_id=run.run_id, examined_count=len(examined), external_skipped_count=external_count
    )
    return ""


def _owner_citation(raw: str) -> str:
    citation = " ".join(raw.split())
    for token in internal_shorthand(citation):
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
        citation = citation.replace(token, replacement)
    safe = escape(citation, quote=False)
    if len(safe) > QUESTION_CITATION_CHARS:
        safe = f"{safe[: QUESTION_CITATION_CHARS - 3].rsplit(' ', 1)[0].rstrip()}..."
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
                    decision=OwnerDecision.PRODUCT_SCOPE,
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
        task.ticket.merge_extra(
            merge_into_dicts={"sweep_fold_proposals": {str(task.pk): {"folds": folds, "run_id": run_id}}}
        )
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
    except Exception as exc:
        message = f"the overlay could not be read ({type(exc).__name__})"
        raise SweepProposalError(message) from exc
    repos = {slug.casefold() for slug in owned_repo_slugs(overlay)}
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
    folds = [_validate_fold(item, examined, index_urls, repos, overlay) for item in folds_raw]
    gaps = [_validate_gap(item, task, index_urls, repos, overlay) for item in gaps_raw]
    return examined, external_count, folds, gaps


def _validated_examined(raw: list[object], repos: set[str]) -> list[str]:
    if any(not _owned_issue_url(url, repos) for url in raw):
        _invalid("examined contains an invalid or unowned issue URL")
    return [str(url) for url in raw]


def _validate_fold(
    raw: object, examined: list[str], index_urls: list[str], repos: set[str], overlay: OverlayBase
) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        _invalid("a fold is not an object")
    member, host, citation = (raw.get(key) for key in ("member_url", "host_url", "citation"))
    if not _owned_issue_url(member, repos) or not _owned_issue_url(host, repos):
        _invalid("a fold has an invalid member or host")
    if member == host or member not in examined or host not in index_urls:
        _invalid("a fold target is not a distinct indexed issue")
    if not isinstance(citation, str) or not citation.strip():
        _invalid("a fold has no citation")
    for url in (member, host):
        _verify_open_self_issue(overlay, url)
    return {"member_url": member, "host_url": host, "citation": citation.strip()}


def _validate_gap(
    raw: object, task: Task, index_urls: list[str], repos: set[str], overlay: OverlayBase
) -> tuple[int, list[str]]:
    if not isinstance(raw, Mapping):
        _invalid("a gap assignment is not an object")
    host_url, keys = raw.get("host_url"), raw.get("gap_keys")
    if not _owned_issue_url(host_url, repos) or host_url not in index_urls:
        _invalid("a gap assignment has no valid host")
    if not isinstance(keys, list) or not keys or any(not isinstance(key, str) or not key.strip() for key in keys):
        _invalid("a gap assignment has no valid keys")
    if not set(keys) <= _offered_gap_keys(task.execution_reason):
        _invalid("a gap assignment names a key not offered in this sweep")
    host_ticket = Ticket.objects.filter(issue_url=host_url, overlay=task.ticket.overlay).first()
    if host_ticket is None:
        _invalid("a gap host is not an owned ticket")
    _verify_open_self_issue(overlay, host_url)
    return host_ticket.pk, keys


def _offered_gap_keys(reason: str) -> set[str]:
    _, marker, manifest = reason.partition(f"{DREAM_BATCH_MANIFEST_HEADER}\n")
    if not marker:
        return set()
    return {line[2:].partition(":")[0].strip() for line in manifest.splitlines() if line.startswith("- ")}


def _verify_open_self_issue(overlay: OverlayBase, url: str) -> None:
    try:
        code_host = get_backend_provider().get_code_host_for_url(overlay, url)
        if code_host is None:
            _invalid("a proposal target has no code host")
        issue = require_self_authored_issue(host=code_host, issue_url=url)
    except Exception as exc:
        message = f"a proposal target could not be verified ({exc})"
        raise SweepProposalError(message) from exc
    if issue.get("state") not in {"open", "opened"}:
        _invalid("a proposal target is not open")


def _owned_issue_url(raw: object, repos: set[str]) -> bool:
    if not isinstance(raw, str):
        return False
    parsed = urlparse(raw)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and not parsed.query
        and not parsed.fragment
        and slug_from_issue_or_pr_url(parsed.path).casefold() in repos
        and bool(repo_namespaced_key_from_path(parsed.path))
    )
