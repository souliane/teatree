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
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class ReviewMessageCacheError(RuntimeError):
    """The review-message record could not be located — ``T3_DATA_DIR`` is unset."""


def _read_existing_payload(path: Path) -> dict[str, dict[str, str]]:
    """The existing record for the ticket, or ``{}`` — tolerating a corrupt file.

    A truncated / non-JSON on-disk record (a partial write, a manual edit) must
    NOT permanently crash this sanctioned post path — the file is a *record*, not
    a dedup oracle, so losing it is recoverable. The corrupt file is renamed aside
    to ``*.corrupt`` (preserved for inspection, never silently deleted) and the
    caller proceeds from an empty record, exactly as :class:`FindingsStore` does.
    """
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        corrupt = path.with_suffix(path.suffix + ".corrupt")
        try:
            path.replace(corrupt)
        except OSError:
            logger.warning("review message cache: could not move corrupt %s aside (%s)", path, exc)
        else:
            logger.warning("review message cache: corrupt %s (%s) — moved to %s, proceeding empty", path, exc, corrupt)
        return {}
    return loaded if isinstance(loaded, dict) else {}


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

    Raises :class:`ReviewMessageCacheError` (a named error, not a bare
    ``KeyError``) when ``T3_DATA_DIR`` is unset, and tolerates a corrupt existing
    record by moving it aside and proceeding empty (see :func:`_read_existing_payload`).
    """
    data_dir = os.environ.get("T3_DATA_DIR")
    if not data_dir:
        msg = "T3_DATA_DIR is not set — cannot locate the review-message record directory."
        raise ReviewMessageCacheError(msg)
    path = Path(data_dir) / "tickets" / iid / "mr_review_messages.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = _read_existing_payload(path)
    payload[mr_url] = {
        "permalink": permalink,
        "channel": channel,
        "ts": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path
