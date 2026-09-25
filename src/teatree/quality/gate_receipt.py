"""Worktree-local verification receipt and PR-visible incomplete marker.

No receipt is an INCOMPLETE verdict: calling ``ship`` or ``ensure-pr`` without
``verify-gates`` must not silently look like a green local check. A green receipt
is valid only for the same clean HEAD; a subsequent commit or dirty tree makes it
incomplete again. The file lives in the worktree's git admin directory, never
in the PR diff.
"""

import datetime as dt
import json
import os
from pathlib import Path

from teatree.utils.run import run_allowed_to_fail

_START = "<!-- t3-verify-gates:start -->"
_END = "<!-- t3-verify-gates:end -->"


def _receipt_path(repo: Path) -> Path:
    result = run_allowed_to_fail(
        ["git", "rev-parse", "--git-path", "t3/verify-gates.json"], cwd=repo, expected_codes=None
    )
    if result.returncode != 0 or not result.stdout.strip():
        message = "cannot resolve git receipt path"
        raise OSError(message)
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else repo / path


def _snapshot(repo: Path) -> tuple[str, bool]:
    head = run_allowed_to_fail(["git", "rev-parse", "HEAD"], cwd=repo, expected_codes=None)
    status = run_allowed_to_fail(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo, expected_codes=None
    )
    if head.returncode != 0 or status.returncode != 0:
        return "", False
    return head.stdout.strip(), not bool(status.stdout.strip())


def write_gate_receipt(repo: Path, *, state: str, reason: str) -> None:
    """Record the last attempted run, replacing any prior green verdict."""
    path = _receipt_path(repo)
    head, clean = _snapshot(repo)
    payload = {
        "state": state,
        "reason": reason.replace("\n", " ")[:200],
        "head": head,
        "clean": clean,
        "at": dt.datetime.now(dt.UTC).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True)


def _gate_status(repo: Path) -> str:
    try:
        payload = json.loads(_receipt_path(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "**Local gates: INCOMPLETE** — no verification receipt for this worktree."
    head, clean = _snapshot(repo)
    if payload.get("state") == "green" and payload.get("head") == head and payload.get("clean") and clean:
        return f"**Local gates: green** — `t3 tool verify-gates` passed on `{head[:12]}`."
    reason = str(payload.get("reason") or "run absent, failed, or stale").replace("\n", " ")[:200]
    return f"**Local gates: INCOMPLETE** — {reason}; rerun `t3 tool verify-gates` on a clean HEAD."


def append_gate_notice(description: str, repo: str | Path) -> str:
    """Attach or refresh one review-visible gate verdict in a PR/MR body."""
    before, marker, rest = description.partition(_START)
    if marker:
        _, end, after = rest.partition(_END)
        description = before.rstrip() + (after if end else "")
    notice = _gate_status(Path(repo))
    return f"{description.rstrip()}\n\n{_START}\n{notice}\n{_END}"


__all__ = ["append_gate_notice", "write_gate_receipt"]
