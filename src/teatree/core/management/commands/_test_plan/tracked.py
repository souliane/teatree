"""The tracked (authored-only) view of a test-plan manifest (teatree #3092).

A run manifest mixes two lifetimes: durable authored intent (workflow names,
human ``steps``, the claim→capture mapping) and ephemeral run provenance
(per-repo commit SHAs, ``missing_on_dev``) that goes stale the moment anything
is pushed. Tracking the whole file in a private test repo therefore churns it on
every run. :func:`strip_run_provenance` drops the top-level ``dev``/``local``
provenance blocks so the committed file is byte-stable across runs; the full
manifest stays out-of-repo for ``post-test-plan`` and provenance stays DB-home
(``Ticket.extra['e2e_recipe']``).
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.core.management.commands._test_plan.render import TestPlanValidationError

if TYPE_CHECKING:
    from teatree.types import RawAPIDict

_PROVENANCE_SIDES = ("dev", "local")
_PROVENANCE_KEYS = ("commits", "missing_on_dev")


def strip_run_provenance(manifest_json: str) -> str:
    """Return *manifest_json* with the ephemeral ``dev``/``local`` run provenance removed.

    Drops the per-repo commit SHAs and ``missing_on_dev`` from the top-level
    side blocks (and a side block that is left with nothing but provenance),
    keeping the authored intent and its key order so two runs that differ only in
    provenance serialise to identical bytes.
    """
    try:
        data = json.loads(manifest_json)
    except json.JSONDecodeError as exc:
        msg = f"--manifest is not valid JSON: {exc}"
        raise TestPlanValidationError(msg) from None
    if not isinstance(data, dict):
        msg = "--manifest must be a JSON object."
        raise TestPlanValidationError(msg)
    tracked: RawAPIDict = {}
    for key, value in data.items():
        if key in _PROVENANCE_SIDES and isinstance(value, dict):
            residual = {k: v for k, v in value.items() if k not in _PROVENANCE_KEYS}
            if residual:
                tracked[key] = residual
            continue
        tracked[key] = value
    return json.dumps(tracked, indent=2, ensure_ascii=False) + "\n"


def run_tracked_manifest(
    manifest: str,
    *,
    write_out: Callable[[str], None],
    write_err: Callable[[str], None],
) -> str:
    """Read a manifest (path or inline JSON), strip its run provenance, and write the result.

    The authored manifest is written to ``write_out`` (and returned) for a
    private test repo to commit. An empty ``--manifest`` or invalid JSON is
    written to ``write_err`` and re-raised as ``SystemExit(1)``.
    """
    if not manifest.strip():
        write_err("--manifest is required (a path to, or inline string of, the test-plan manifest JSON).")
        raise SystemExit(1)
    path = Path(manifest)
    manifest_json = path.read_text(encoding="utf-8") if path.is_file() else manifest
    try:
        tracked = strip_run_provenance(manifest_json)
    except TestPlanValidationError as err:
        write_err(str(err))
        raise SystemExit(1) from err
    write_out(tracked)
    return tracked
