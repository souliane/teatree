"""Django-free local evidence for hook gate decisions."""

import hashlib
import json
import os
import re
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from hooks.scripts.loop_registry_path import loop_registry_path

_MARKER = re.compile(r"[a-z0-9_.:-]{1,64}\Z")
_DECISIONS = frozenset({"deny", "override", "advise"})
RETENTION_DAYS = 7
# These are literal, public markers declared by hook call sites. A token-shaped
# prefix from a private reason is never telemetry, even if it matches _MARKER.
DECLARED_GATES = frozenset(
    {
        "ai_signature",
        "banned_terms",
        "block-standing-grant-ask",
        "block-cron-loop-shell",
        "brief_anchor",
        "classifier_relax",
        "config_overwrite",
        "deferred_question",
        "diff_coverage",
        "direct_command",
        "dispatch_admission",
        "dispatch_quote_scanner",
        "foreign_branch_push",
        "foreign_branch_push_force_delete",
        "general_purpose_agent_gate",
        "git_add_all",
        "glab_stale_base_remote",
        "headless_authoring",
        "main_clone",
        "mcp_slack_write",
        "mr_metadata",
        "no_self_reviewer_assign",
        "orchestrator_bash",
        "orchestrator_delegation_gate",
        "orchestrator_foreground_dispatch",
        "out_of_band_merge",
        "plan_gate",
        "protect_default_branch",
        "quote_scanner",
        "raw_issue_write",
        "raw_pid_kill",
        "raw_review_post",
        "secret_file_print",
        "self_dm",
        "single_branch_repo",
        "skill-loading-enforcement",
        "unapprovable_author_create",
        "unbounded_wait",
        "unknown_repo_push",
        "verbatim_operator_paste",
        "visible_plan_gate",
    }
)
_PUBLIC_REASON_MARKERS = frozenset({"blocked", "skill-loading-enforcement"})


def session_ref(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16] if session_id else ""


def _directory() -> Path:
    return loop_registry_path().parent / "otel"


def _marker(value: str, *, allowed: frozenset[str]) -> str:
    return value if value in allowed and _MARKER.fullmatch(value) else "unknown"


def _id(size: int) -> str:
    raw = os.urandom(size)
    return (raw if any(raw) else b"\x00" * (size - 1) + b"\x01").hex()


def write_gate_decision(*, gate: str, decision: str, rule: str, session_id: str) -> None:
    if decision not in _DECISIONS:
        return

    epoch = int(time.time())
    row: dict[str, int | str] = {
        "epoch": epoch,
        "trace_id": _id(16),
        "span_id": _id(8),
        "gate": _marker(gate, allowed=DECLARED_GATES | _PUBLIC_REASON_MARKERS),
        "decision": decision,
        "rule": _marker(rule, allowed=DECLARED_GATES | _PUBLIC_REASON_MARKERS),
    }
    if ref := session_ref(session_id):
        row["session_ref"] = ref

    directory = _directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"gate-{datetime.fromtimestamp(epoch, UTC).date().isoformat()}.jsonl"
    if not path.exists():
        cutoff = datetime.fromtimestamp(epoch, UTC).date() - timedelta(days=RETENTION_DAYS)
        for old in directory.glob("gate-????-??-??.jsonl"):
            try:
                if date.fromisoformat(old.stem[5:]) < cutoff:
                    old.unlink()
            except (OSError, ValueError):
                pass
    payload = (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.write(fd, payload) != len(payload):
            raise OSError
    finally:
        os.close(fd)
