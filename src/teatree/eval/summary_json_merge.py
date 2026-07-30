"""Merge per-shard publish-safe ``--summary-json`` artifacts into one (§2.4).

The CI heal workflow shards the full suite across a parallel matrix; each shard
uploads its own publish-safe per-scenario ``--summary-json`` (the same §2.4 schema
:func:`teatree.eval.summary_json.render_summary_json` produces). This module reads
every per-shard JSON (a directory or explicit paths) and folds them into ONE
``eval-heal-<sha>`` payload with the identical schema: the ``totals`` summed and
the ``scenarios`` concatenated, so the existing ``t3 eval ci-status`` download
path reads the combined run exactly as it read a single-invocation run.

Only the already-sanitized per-shard rows are read — no transcript ever enters
here, so the merged artifact stays publish-safe by construction. ``head_sha`` and
``generated_at`` are PASSED IN (never computed here), mirroring
:func:`teatree.eval.summaries.merge_summaries`, so the merge is deterministic and
unit-testable.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from teatree.eval.triage import ScenarioRecord

_TOTALS_KEYS = ("total", "passed", "failed", "skipped")


def summary_json_files(inputs: list[str]) -> list[Path]:
    """Expand *inputs* (files or directories) to the per-shard ``*.json`` paths, sorted."""
    paths: list[Path] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            paths.extend(sorted(path.glob("*.json")))
        elif path.is_file():
            paths.append(path)
    return paths


def merge_summary_payloads(
    payloads: Sequence[Mapping[str, Any]], *, head_sha: str, generated_at: str
) -> dict[str, Any]:
    """Fold per-shard §2.4 payloads into one — totals summed, scenarios concatenated.

    A shard with distinct ``model`` values is joined into a sorted comma list so
    the field stays informative and deterministic; a run whose shards all agree
    keeps its single model string.
    """
    scenarios: list[ScenarioRecord] = []
    totals = dict.fromkeys(_TOTALS_KEYS, 0)
    for payload in payloads:
        shard_scenarios = payload.get("scenarios")
        if isinstance(shard_scenarios, list):
            scenarios.extend(shard_scenarios)
        shard_totals = payload.get("totals")
        if isinstance(shard_totals, Mapping):
            for key in _TOTALS_KEYS:
                totals[key] += int(shard_totals.get(key, 0))
    models = sorted({str(payload.get("model", "")) for payload in payloads} - {"", "unknown"})
    return {
        "generated_at": generated_at,
        "model": ",".join(models) if models else "unknown",
        "head_sha": head_sha,
        "totals": totals,
        "scenarios": scenarios,
    }


def merge_summary_json(inputs: list[str], *, head_sha: str, generated_at: str) -> str:
    """Read every per-shard summary JSON and render the merged §2.4 JSON string."""
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in summary_json_files(inputs)]
    merged = merge_summary_payloads(payloads, head_sha=head_sha, generated_at=generated_at)
    return json.dumps(merged, indent=2)
