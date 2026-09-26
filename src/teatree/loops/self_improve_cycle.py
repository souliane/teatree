"""The worker's self-improve cycle, run through the ``loop_self_improve`` command.

The command owns the lease, owner delivery and the scan report; the chain only runs
it as the loop runner and folds its JSON into the timer result.
"""

import io
import json
import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager

from django.core.management import call_command

from teatree.core.session_identity import runner_identity_env

logger = logging.getLogger(__name__)

SELF_IMPROVE_TIER = "cheap"
_SAFE_SCAN_LABEL = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _scan_label(value: object) -> str:
    return value if isinstance(value, str) and _SAFE_SCAN_LABEL.fullmatch(value) else "unknown"


@contextmanager
def _as_loop_runner() -> Iterator[None]:
    """Run as the worker's own principal, which the t3-master gate never treats as a rival."""
    identity = runner_identity_env(os.getpid())
    saved = {key: os.environ.get(key) for key in identity}
    os.environ.update(identity)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_self_improve_cycle_via_command() -> dict[str, int]:
    """Use the command's t3-master gate, lease, owner delivery, and scan report."""
    output = io.StringIO()
    with _as_loop_runner():
        call_command("loop_self_improve", tier=SELF_IMPROVE_TIER, json_output=True, stdout=output, stderr=io.StringIO())
    payload = json.loads(output.getvalue())
    if not isinstance(payload, dict):
        msg = "self-improve command did not return a JSON object"
        raise TypeError(msg)
    degraded = payload.get("degraded_scans")
    if isinstance(degraded, list) and degraded:
        summary = ",".join(
            f"{_scan_label(row.get('detector'))}:{_scan_label(row.get('reason'))}"
            for row in degraded[:5]
            if isinstance(row, dict)
        )
        logger.warning("unattended self-improve scan degraded (%d sources): %s", len(degraded), summary)
        return {"ran": 1, "degraded": len(degraded)}
    if payload.get("skipped") or payload.get("skipped_reason"):
        logger.warning("unattended self-improve cycle skipped")
        return {"skipped": 1}
    return {"ran": 1}
