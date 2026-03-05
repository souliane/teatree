#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "typer>=0.12",
# ]
# ///
"""Check ticket transition gates from followup.json.

Gates:
- Doing → Technical Review: all MRs have review request messages
- Technical Review → DEV Review: all MRs merged + deployed

Used by: t3-followup (§9 transition checks, §10 status check mode).
Output: JSON with gate status per ticket.
"""

import json
import os
import sys
from pathlib import Path

import typer

_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from lib.gitlab import get_mr, get_mr_state

_DEFAULT_DATA_DIR = Path.home() / ".local/share/teatree"


def _data_dir() -> Path:
    return Path(os.environ.get("T3_DATA_DIR") or str(_DEFAULT_DATA_DIR))


def _check_doing_to_review(ticket: dict, mrs_data: dict) -> dict:
    """Check Doing → Technical Review gate: all MRs have review requests."""
    mr_keys = ticket.get("mrs", [])
    if not mr_keys:
        return {"ready": False, "reason": "No MRs found"}

    total = len(mr_keys)
    reviewed = 0
    details = []

    for mr_key in mr_keys:
        mr = mrs_data.get(mr_key, {})
        if mr.get("skipped"):
            total -= 1
            details.append(f"{mr_key}: skipped ({mr.get('skip_reason', '?')})")
            continue
        if mr.get("review_requested"):
            reviewed += 1
            details.append(f"{mr_key}: review requested")
        else:
            details.append(f"{mr_key}: NOT sent for review")

    if total == 0:
        return {"ready": False, "reason": "All MRs skipped"}

    ready = reviewed == total
    return {
        "ready": ready,
        "reason": f"{reviewed}/{total} MRs have review requests",
        "details": details,
    }


def _check_review_to_dev(ticket: dict, mrs_data: dict) -> dict:  # noqa: C901, PLR0912
    """Check Technical Review → DEV Review gate: all MRs merged + deployed."""
    mr_keys = ticket.get("mrs", [])
    if not mr_keys:
        return {"ready": False, "reason": "No MRs found"}

    total = len(mr_keys)
    merged = 0
    details = []

    for mr_key in mr_keys:
        mr = mrs_data.get(mr_key, {})
        if mr.get("skipped"):
            total -= 1
            details.append(f"{mr_key}: skipped")
            continue

        project_id = mr.get("project_id")
        # Parse IID from key
        iid_str = mr_key.split("!")[-1] if "!" in mr_key else ""
        if not project_id or not iid_str:
            details.append(f"{mr_key}: missing project_id")
            continue

        state = get_mr_state(project_id, int(iid_str))
        if state and state.get("state") == "merged":
            merged += 1
            details.append(f"{mr_key}: merged")
        else:
            current_state = state.get("state", "unknown") if state else "unknown"
            details.append(f"{mr_key}: {current_state}")

    if total == 0:
        return {"ready": False, "reason": "All MRs skipped"}

    all_merged = merged == total

    # If all merged, check deployment via extension point
    deployed = False
    if all_merged:
        try:
            from lib.init import init

            init()
            from lib.registry import call as ext_call

            mrs_for_deploy = []
            for mr_key in mr_keys:
                mr = mrs_data.get(mr_key, {})
                if not mr.get("skipped"):
                    iid_str = mr_key.split("!")[-1]
                    full_mr = get_mr(mr["project_id"], int(iid_str))
                    if full_mr:
                        mrs_for_deploy.append(full_mr)

            deployed = ext_call("ticket_check_deployed", ticket.get("_iid", ""), mrs_for_deploy)
        except (ImportError, TypeError):
            deployed = False
            details.append("deployment check: extension point not available")

    if all_merged and deployed:
        return {"ready": True, "reason": "All MRs merged and deployed", "details": details}
    if all_merged:
        return {"ready": False, "reason": f"All merged but not deployed ({merged}/{total})", "details": details}
    return {"ready": False, "reason": f"{merged}/{total} MRs merged", "details": details}


def check_gates(followup_path: str = "") -> dict:
    """Check all transition gates. Returns {ticket_id: {current, target, gate}}."""
    path = Path(followup_path) if followup_path else _data_dir() / "followup.json"
    if not path.is_file():
        return {"error": "followup.json not found"}

    data = json.loads(path.read_text(encoding="utf-8"))
    tickets = data.get("tickets", {})
    mrs_data = data.get("mrs", {})

    results: dict = {}
    for ticket_id, ticket in tickets.items():
        status = ticket.get("tracker_status", "")

        if "Doing" in (status or ""):
            gate = _check_doing_to_review(ticket, mrs_data)
            results[ticket_id] = {
                "title": ticket.get("title", ""),
                "current": status,
                "target": "Process::Technical Review",
                "gate": gate,
            }
        elif "Technical" in (status or "") and "review" in (status or "").lower():
            gate = _check_review_to_dev(ticket, mrs_data)
            results[ticket_id] = {
                "title": ticket.get("title", ""),
                "current": status,
                "target": "Process::DEV Review",
                "gate": gate,
            }

    return results


def main(
    followup: str = typer.Argument("", help="Path to followup.json (default: $T3_DATA_DIR/followup.json)"),
) -> None:
    """Check ticket transition gates."""
    results = check_gates(followup)

    if "error" in results:
        print(f"ERROR: {results['error']}", file=sys.stderr)
        raise SystemExit(1)

    if not results:
        print("No tickets with pending transitions")
        return

    print(json.dumps(results, indent=2, ensure_ascii=False))

    # Summary table
    print("\n--- Transition Summary ---", file=sys.stderr)
    for tid, info in results.items():
        ready = info["gate"]["ready"]
        symbol = "YES" if ready else "NO"
        reason = info["gate"]["reason"]
        print(f"  #{tid}: {info['current']} → {info['target']}  [{symbol}] {reason}", file=sys.stderr)


if __name__ == "__main__":
    typer.run(main)
