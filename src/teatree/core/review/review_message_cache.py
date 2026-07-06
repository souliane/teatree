"""Per-ticket record of where a sanctioned review-request post landed (#1098).

NOT a dedup oracle. Duplicate suppression stays the #1084
``review_request_guard`` live-channel read + atomic ``ReviewRequestPost``
claim. This file is a durable *record* of the resulting permalink so it
survives outside Slack (the loop's nag/reconcile state lives in the DB,
not here).

Kept separate from the management command so the read-merge-write JSON
contract — accumulate one entry per MR URL, never clobber a sibling MR's
entry — is unit-testable without ``call_command``.
"""

import json
import os
from datetime import datetime
from pathlib import Path


def persist_review_message(
    *,
    mr_url: str,
    iid: str,
    permalink: str,
    channel: str,
    when: datetime,
) -> Path:
    """Record ``mr_url``'s review-message permalink under the ticket ``iid``.

    Reads any existing ``mr_review_messages.json`` for the ticket, merges
    in (or overwrites) only this MR's entry, and writes it back — sibling
    MR entries in the same ticket file are preserved verbatim. Returns the
    file path written.
    """
    path = Path(os.environ["T3_DATA_DIR"]) / "tickets" / iid / "mr_review_messages.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, dict[str, str]] = {}
    if path.exists():
        payload = json.loads(path.read_text())

    payload[mr_url] = {
        "permalink": permalink,
        "channel": channel,
        "ts": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path
