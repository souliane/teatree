"""Teatree-own MCP write tools (#3076) — each handler calls the seam the `t3` CLI calls.

The structural rule (BLUEPRINT § Per-service declared tool groups): an MCP
write handler never touches a transport (gh/glab argv, raw httpx, slack_sdk)
directly — it calls the exact seam the corresponding ``t3`` command calls, so
every gate (shipping-phase FSM, sanctioned-merge keystone, live-post approval,
on-behalf verdict, banned-terms / leak guards, close-trailer scrub) fires
identically on both surfaces. Command-shaped writes go through
``django.core.management.call_command`` — the literal CLI code path — and the
review posts go through the :mod:`teatree.mcp.review_seam` registration seam.
``TOOL_SEAMS`` names each tool's seam; the transport-boundary fitness test
(``tests/teatree_mcp/test_transport_boundary.py``) pins both the mapping's
coverage and the no-transport-import rule.

Gate-satisfier commands (``review approve-on-behalf``, ``review
approve-live-post``, ``ticket e2e-bypass``, ``recipe approve``, DB-refresh
approvals) are deliberately NOT exposed as tools — exposing them would let the
agent self-approve (maker≠checker).
"""

import contextlib
import io
import json
from fnmatch import fnmatch
from typing import Any, cast

import typer
from asgiref.sync import sync_to_async
from django.core.management import call_command
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from teatree.config.cold_hook_settings import COLD_HOOK_SETTINGS
from teatree.config.feature_flags import is_feature_flag
from teatree.config.registries import COLD_SETTINGS, REGISTRY_KEYS
from teatree.core.models import Task
from teatree.core.notify import NotifyKind, notify_user
from teatree.mcp.review_seam import review_post_seam

_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def _run_command(command: str, *args: object, **kwargs: object) -> object:
    """Run a management command as the CLI does, but surface its error primitive.

    The wrapped commands signal input errors with ``SystemExit`` / ``typer.Exit``
    — a ``BaseException``. FastMCP only converts ``Exception`` to a structured
    ``ToolError``, so an unguarded exit would crash the whole tool call instead of
    returning the documented refusal. Capture the command's stderr and re-raise as
    a plain ``RuntimeError`` so the caller gets the message, not a dead session.
    """
    err = io.StringIO()
    try:
        return call_command(command, *args, stderr=err, **kwargs)
    except (SystemExit, typer.Exit) as exc:
        code = getattr(exc, "code", None)
        if code is None:
            code = getattr(exc, "exit_code", 1)
        message = err.getvalue().strip() or f"command failed (exit {code})"
        raise RuntimeError(message) from exc


def _last_json_object(text: str) -> dict[str, Any] | None:
    """The last stdout line that parses as a JSON object, or ``None``."""
    for raw in reversed(text.strip().splitlines()):
        line = raw.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        with contextlib.suppress(json.JSONDecodeError):
            return cast("dict[str, Any]", json.loads(line))
    return None


def _run_emitting_command(command: str, *args: object, **kwargs: object) -> dict[str, Any]:
    """Run a command that reports its verdict via one JSON line + ``SystemExit``.

    ``review_request_post`` prints a single machine-legible JSON dict
    (``action`` ∈ post/draft/suppress/refused) to stdout and terminates via
    ``SystemExit`` (0 for post/draft/suppress, 2 for refused). Capture stdout and
    return the parsed verdict — the ``action`` field carries the outcome, so the
    exit code is not needed. Surface stderr as a structured ``RuntimeError`` when
    the command emitted no JSON (so a FastMCP tool call is never crashed by the
    ``SystemExit`` primitive the CLI uses).
    """
    out = io.StringIO()
    err = io.StringIO()
    with (
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
        contextlib.suppress(SystemExit, typer.Exit),
    ):
        call_command(command, *args, **kwargs)
    payload = _last_json_object(out.getvalue())
    if payload is not None:
        return payload
    message = err.getvalue().strip() or out.getvalue().strip() or f"{command} produced no machine-readable output"
    raise RuntimeError(message)


# Safety-gate keys an MCP caller may never flip: cold-hook gate wires, feature
# flags (directive-/lifecycle-governed), the opt-in ``require_*`` training
# wheels, ``*_gate_enabled`` kill-switches, the registry rows (``overlays`` /
# ``e2e_repos`` redirect overlay code paths), and the cold-read ``COLD_SETTINGS``
# — the leak-scrub input lists (``banned_terms`` / ``banned_brands`` /
# ``overlay_leak_terms`` …), the master ``danger_gate_fail_open`` switch, and the
# agent-routing tables. Emptying a leak-scrub list or flipping fail-open over MCP
# would neuter a live guard; those stay a human/CLI act — a TIGHTENING over the
# Bash ``t3 <overlay> config_setting set`` path, per the no-unilateral-gate-flip rule.
_REFUSED_KEY_GLOBS = ("*_gate_enabled", "require_*")

TOOL_SEAMS: dict[str, str] = {
    "pr_create": "call_command('pr', 'create', …, sync=True) — full ship-gate chain",
    "pr_merge": "call_command('ticket', 'merge', <clear_id>) — sanctioned keystone merge",
    "ticket_visit_phase": "call_command('lifecycle', 'visit-phase', …) — phase gates",
    "record_e2e_run": "call_command('lifecycle', 'record-e2e-run', …) — e2e attestation",
    "config_setting_set": "call_command('config_setting', 'set', …) + gate-key refuse list",
    "task_create": "call_command('tasks', 'create', …) — dispatch-quote gate wiring",
    "task_complete": "teatree.core.models.Task.complete",
    "task_fail": "teatree.core.models.Task.fail",
    "notify_user": "teatree.core.notify.notify_user — send-proxy + BotPing audit + own-DM carve-out",
    "question_answer": "call_command('questions', 'answer', …)",
    "worktree_teardown": "call_command('workspace', 'teardown', …) — liveness/dirty guards",
    "review_post_draft_note": "teatree.mcp.review_seam (ReviewService.post_draft_note)",
    "review_post_comment": "teatree.mcp.review_seam (ReviewService.post_comment; live gated #1207)",
    "review_request_post": "call_command('review_request_post') — #1094 dedup + #960 on-behalf + review-state",
    "slack_react": "OnBehalfSlackEgress.react — #117 send-proxy + on-behalf gate + notify receipt",
    "github_issue_create": "code_host_from_overlay().create_issue via the leak scrub + #117 send-proxy",
    "github_issue_comment": "code_host_from_overlay().post_issue_comment via the leak scrub + #117 send-proxy",
    "github_issue_close": "code_host_from_overlay().close_issue via the leak scrub + #117 send-proxy",
    "github_issue_update": "code_host_from_overlay().update_issue via the leak scrub + #117 send-proxy",
    "gitlab_issue_create": "code_host_from_overlay().create_issue via the leak scrub + #117 send-proxy",
    "gitlab_issue_comment": "code_host_from_overlay().post_issue_comment via the leak scrub + #117 send-proxy",
    "gitlab_issue_close": "code_host_from_overlay().close_issue via the public-repo leak scrub + send-proxy",
    "gitlab_issue_update": "code_host_from_overlay().update_issue via the public-repo leak scrub + send-proxy",
}

INSTRUCTIONS = (
    "- pr_create(ticket, title): create the ticket's PR through the full "
    "ship-gate chain (shipping-phase FSM, visual QA, title validator, budget/debt "
    "gates). Errors report the exact failing gate.\n"
    "- pr_merge(clear_id): execute a sanctioned keystone merge for an ISSUED "
    "MergeClear — sha-bound, CI-rollup-checked, maker≠checker enforced. There is "
    "no raw-merge tool; issuing the CLEAR stays on the CLI.\n"
    "- ticket_visit_phase(ticket, phase, agent_id): record a lifecycle phase "
    "visit (same phase gates as `t3 <overlay> lifecycle visit-phase`).\n"
    "- record_e2e_run(ticket, spec, result, head_sha, posted_url): record the "
    "mandatory-E2E attestation (posted evidence URL required to clear the gate).\n"
    "- config_setting_set(key, value, overlay): set a plain config setting; "
    "REFUSES safety-gate keys (*_gate_enabled, require_*, feature flags, "
    "cold-hook wires, registry rows, and the cold-read leak-scrub lists / "
    "fail-open switch / agent-routing tables) — those stay human/CLI-only.\n"
    "- task_create(ticket, phase, reason, kind, interactive): enqueue the "
    "next-phase task for a ticket through the `tasks create` seam (same "
    "dispatch-quote gate wiring). A bad ticket / missing phase reports the "
    "command's own message as a structured error.\n"
    "- task_complete(task_id, result_artifact_path) / task_fail(task_id): loop "
    "task bookkeeping (complete advances the ticket FSM).\n"
    "- notify_user(text, kind, idempotency_key): send a bot→user DM through the "
    "audited notify egress (send-proxy + BotPing idempotency + own-DM "
    "never-lockout carve-out). Pass a stable idempotency_key to dedupe retries.\n"
    "- question_answer(question_id, text, resolver): answer a pending "
    "DeferredQuestion (single-use, audited).\n"
    "- worktree_teardown(path, force): tear down the ticket workspace *path* "
    "resolves to (every worktree in it; refuses live or dirty trees unless forced).\n"
    "- review_post_draft_note(repo, mr, note): colleague-INVISIBLE MR-level "
    "draft note — always safe, gate-exempt by design.\n"
    "- review_post_comment(repo, mr, note, live): MR-level, DRAFT by default; "
    "live=true requires the recorded LivePostApproval + on-behalf verdict, same "
    "as the CLI.\n"
    "- review_request_check(mr_url): race-safe pre-post dedup PEEK (takes no "
    "claim). Returns action=post|suppress; ABORT the post on suppress.\n"
    "- review_request_post(mr_url, approver, title, ticket_id, head_sha): post a "
    "review request through the #1094 dedup + #960 on-behalf + review-state gate "
    "chain. Returns action=post|draft|suppress|refused; refused names the missing "
    "recorded approval / attestation."
)


def refuse_reason(key: str) -> str:
    """Why the MCP surface refuses to set *key* (empty string = allowed)."""
    if key in COLD_HOOK_SETTINGS:
        return "cold-hook gate wire — flip via the CLI, never via MCP"
    if is_feature_flag(key):
        return "feature flag — directive-/lifecycle-governed, human/CLI-only"
    if key in REGISTRY_KEYS:
        return "registry row — redirects overlay code paths, human/CLI-only"
    if key in COLD_SETTINGS:
        return "cold-read key — leak-scrub list / fail-open switch / agent routing, human/CLI-only"
    if any(fnmatch(key, glob) for glob in _REFUSED_KEY_GLOBS):
        return "safety-gate key — flip via the CLI, never via MCP"
    return ""


async def _pr_create(ticket: str, *, title: str = "") -> dict[str, Any]:
    """Create the ticket's PR through the full ship-gate chain.

    Wraps ``t3 <overlay> pr create <ticket> --sync`` in-process: the shipping
    gate (testing + reviewing phases visited), visual QA, the title/description
    validator, ticket-URL injection, PR-budget and debt-delta gates all run. A
    blocked gate returns the structured failure naming the missing evidence.
    """
    return await sync_to_async(
        lambda: cast("dict[str, Any]", _run_command("pr", "create", ticket, title=title, sync=True)),
        thread_sensitive=True,
    )()


async def _pr_merge(clear_id: int) -> dict[str, Any]:
    """Execute the sanctioned keystone merge for an issued MergeClear.

    Wraps ``t3 <overlay> ticket merge <clear_id>``: re-reads the CLEAR, verifies
    live head SHA == reviewed sha, live required-checks green, not-draft,
    maker≠checker, substrate hold — then performs the SHA-bound squash merge and
    records MergeAudit + FSM advance. Issuing a CLEAR is NOT possible over MCP.
    """
    return await sync_to_async(
        lambda: cast("dict[str, Any]", _run_command("ticket", "merge", str(clear_id))),
        thread_sensitive=True,
    )()


async def _ticket_visit_phase(ticket: str, phase: str, *, agent_id: str = "") -> dict[str, Any]:
    """Record a lifecycle phase visit for the ticket (pk / issue number / URL).

    Same normalization and phase gates as ``t3 <overlay> lifecycle visit-phase``
    (review-skill evidence, review-context, reviewer attestation on
    ``reviewing``); an illegal transition reports the resulting state loudly.
    """
    return await sync_to_async(
        lambda: {"message": str(_run_command("lifecycle", "visit-phase", ticket, phase, agent_id=agent_id))},
        thread_sensitive=True,
    )()


async def _record_e2e_run(
    ticket: str,
    *,
    spec: str = "",
    result: str = "green",
    head_sha: str = "",
    posted_url: str = "",
) -> dict[str, Any]:
    """Record the mandatory-E2E attestation for the ticket.

    Wraps ``t3 <overlay> lifecycle record-e2e-run``. A green run recorded
    without ``posted_url`` does NOT satisfy the gate — the posted evidence URL
    is the part that clears it.
    """
    return await sync_to_async(
        lambda: cast(
            "dict[str, Any]",
            _run_command(
                "lifecycle",
                "record-e2e-run",
                ticket,
                spec=spec,
                result=result,
                head_sha=head_sha,
                posted_url=posted_url,
            ),
        ),
        thread_sensitive=True,
    )()


async def _config_setting_set(key: str, value: str, *, overlay: str = "") -> dict[str, Any]:
    """Set a plain config setting (JSON value) — safety-gate keys are refused.

    Wraps ``t3 <overlay> config_setting set`` (same registry validation, same
    canonical-value storage). Gate keys (``*_gate_enabled``, ``require_*``,
    feature flags, cold-hook wires, registry rows, and the cold-read leak-scrub
    lists / fail-open switch / agent-routing tables) are refused: flipping a
    safety gate — or emptying a leak-scrub list — stays a human/CLI act.
    """
    if reason := refuse_reason(key):
        msg = f"refused: {key} is not MCP-settable ({reason})"
        raise ValueError(msg)

    def _set() -> dict[str, Any]:
        _run_command("config_setting", "set", key, value, overlay=overlay)
        return {"ok": True, "key": key, "overlay": overlay}

    return await sync_to_async(_set, thread_sensitive=True)()


async def _task_create(
    ticket: int,
    *,
    phase: str = "",
    reason: str = "",
    kind: str = "",
    interactive: bool = False,
) -> dict[str, Any]:
    """Enqueue the next-phase task for a ticket — the MCP mirror of ``tasks create``.

    Wraps ``t3 <overlay> tasks create <ticket> --phase … --reason …`` so the
    dispatch-quote gate wiring keeps its semantics. A missing/blank phase, empty
    reason, or unknown ticket surfaces the command's own message as a structured
    error (``_run_command`` converts the command's ``SystemExit`` so the tool
    call is never killed).
    """
    return await sync_to_async(
        lambda: {
            "ok": True,
            **cast(
                "dict[str, Any]",
                _run_command("tasks", "create", ticket, phase=phase, reason=reason, kind=kind, interactive=interactive),
            ),
        },
        thread_sensitive=True,
    )()


async def _notify_user(text: str, *, kind: str = "info", idempotency_key: str) -> dict[str, Any]:
    """Send a bot→user DM through the audited notify egress.

    Wraps :func:`teatree.core.notify.notify_user` — the single notification
    egress with the send-proxy, the BotPing idempotency ledger, and the own-DM
    never-lockout carve-out. Pass a stable ``idempotency_key`` so a retry under
    the same key is a no-op rather than a duplicate DM. Returns ``sent=false``
    when the feature is disabled or no messaging backend / user id is configured.
    """
    sent = await sync_to_async(
        lambda: notify_user(text, kind=NotifyKind(kind), idempotency_key=idempotency_key),
        thread_sensitive=True,
    )()
    return {"ok": bool(sent), "sent": bool(sent), "idempotency_key": idempotency_key}


async def _task_complete(task_id: int, *, result_artifact_path: str = "") -> dict[str, Any]:
    """Complete a loop task — records the phase visit and advances the ticket FSM."""

    def _complete() -> dict[str, Any]:
        task = Task.objects.get(pk=task_id)
        task.complete(result_artifact_path=result_artifact_path)
        return {"ok": True, "task_id": task_id, "status": task.status}

    return await sync_to_async(_complete, thread_sensitive=True)()


async def _task_fail(task_id: int) -> dict[str, Any]:
    """Fail a loop task — clears the claim without advancing the ticket FSM."""

    def _fail() -> dict[str, Any]:
        task = Task.objects.get(pk=task_id)
        task.fail()
        return {"ok": True, "task_id": task_id, "status": task.status}

    return await sync_to_async(_fail, thread_sensitive=True)()


async def _question_answer(question_id: int, text: str, *, resolver: str = "mcp") -> dict[str, Any]:
    """Answer a pending DeferredQuestion (single-use CAS + audit row).

    Wraps ``t3 teatree questions answer`` — resumes any parked headless task
    with the answer, exactly like the CLI.
    """

    def _answer() -> dict[str, Any]:
        _run_command("questions", "answer", question_id, text, resolver_id=resolver)
        return {"ok": True, "question_id": question_id}

    return await sync_to_async(_answer, thread_sensitive=True)()


async def _worktree_teardown(path: str, *, force: bool = False) -> str:
    """Tear down the ticket workspace *path* resolves to (bounded-duration; provisioning stays CLI).

    Wraps ``t3 <overlay> workspace teardown``, which resolves the ticket from
    *path* and tears down every worktree in that ticket's workspace — the FSM
    transition plus the cleanup runner with its liveness, dirty-tree, and
    unpushed-commit guards (a dirty tree without ``force`` refuses).
    """
    return await sync_to_async(
        lambda: str(_run_command("workspace", "teardown", path=path, force=force)),
        thread_sensitive=True,
    )()


async def _review_post_draft_note(repo: str, mr: int, note: str) -> dict[str, Any]:
    """Post a colleague-INVISIBLE MR-level draft review note — always safe, gate-exempt.

    Routes through the registered review seam (the exact ``t3 review
    post-draft-note`` service), so the shape / bloat / banned-terms pre-publish
    gates still apply. Inline (file/line) anchoring stays on the CLI for now.
    """
    message, code = await sync_to_async(
        lambda: review_post_seam().post_draft_note(repo, mr, note),
        thread_sensitive=True,
    )()
    return {"message": message, "code": code}


async def _review_post_comment(repo: str, mr: int, note: str, *, live: bool = False) -> dict[str, Any]:
    """Post an MR-level comment — DRAFT by default; ``live=true`` stays approval-gated.

    Routes through the registered review seam (the exact ``t3 review
    post-comment`` service): ``live=true`` requires the single-use
    LivePostApproval (#1207) plus the on-behalf verdict, identically to the CLI.
    Inline (file/line) anchoring stays on the CLI for now.
    """
    message, code = await sync_to_async(
        lambda: review_post_seam().post_comment(repo, mr, note, live=live),
        thread_sensitive=True,
    )()
    return {"message": message, "code": code}


async def _review_request_check(mr_url: str) -> dict[str, Any]:
    """Peek POST-or-SUPPRESS for a review-request without taking a claim (#1084).

    Wraps ``t3 review-request check``: reads the live review channel with the
    same token the post would use so a duplicate (agent re-post or a manual
    out-of-band post) is detected. Decision-only — takes NO durable claim, so it
    can never wedge a later real post. The caller MUST abort on ``suppress``.
    """
    return await sync_to_async(
        lambda: cast("dict[str, Any]", _run_command("review_request_check", "--mr-url", mr_url)),
        thread_sensitive=True,
    )()


async def _review_request_post(
    mr_url: str,
    approver: str,
    *,
    title: str = "",
    ticket_id: str = "",
    head_sha: str = "",
) -> dict[str, Any]:
    """Post a review request through the #1094 dedup + #960 on-behalf + review-state gates.

    Wraps ``t3 review-request post``: the anti-vacuity + reviewed-state gates run
    first, then the live-channel dedup claim, then the #960 on-behalf approval
    (no recorded, unconsumed, exactly-scoped approval ⇒ ``refused`` with the
    ``t3 review approve-on-behalf`` remediation and the orphan claim rolled back),
    then the post. Returns the command's machine-legible verdict.
    """
    return await sync_to_async(
        lambda: _run_emitting_command(
            "review_request_post",
            "--mr-url",
            mr_url,
            "--approver",
            approver,
            "--title",
            title,
            "--ticket-id",
            ticket_id,
            "--head-sha",
            head_sha,
        ),
        thread_sensitive=True,
    )()


def register(server: FastMCP) -> None:
    server.add_tool(_pr_create, name="pr_create", annotations=_WRITE)
    server.add_tool(_pr_merge, name="pr_merge", annotations=_DESTRUCTIVE)
    server.add_tool(_ticket_visit_phase, name="ticket_visit_phase", annotations=_WRITE)
    server.add_tool(_record_e2e_run, name="record_e2e_run", annotations=_WRITE)
    server.add_tool(_config_setting_set, name="config_setting_set", annotations=_WRITE)
    server.add_tool(_task_create, name="task_create", annotations=_WRITE)
    server.add_tool(_task_complete, name="task_complete", annotations=_WRITE)
    server.add_tool(_task_fail, name="task_fail", annotations=_DESTRUCTIVE)
    server.add_tool(_notify_user, name="notify_user", annotations=_WRITE)
    server.add_tool(_question_answer, name="question_answer", annotations=_WRITE)
    server.add_tool(_worktree_teardown, name="worktree_teardown", annotations=_DESTRUCTIVE)
    server.add_tool(_review_post_draft_note, name="review_post_draft_note", annotations=_WRITE)
    server.add_tool(_review_post_comment, name="review_post_comment", annotations=_WRITE)
    server.add_tool(_review_request_check, name="review_request_check", annotations=_READ_ONLY)
    server.add_tool(_review_request_post, name="review_request_post", annotations=_WRITE)
