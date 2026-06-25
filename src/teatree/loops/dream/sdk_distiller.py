"""The real LLM distiller: extract → root-cause clusters via one headless SDK call.

This is the only place the dream pass touches an LLM. :func:`sdk_distiller` is the
default :class:`~teatree.loops.dream.engine.Distiller` the engine injects; tests pass a
fake so the engine — and every phase around it — runs with no LLM. The concern split out
of :mod:`teatree.loops.dream.engine` (#2723) is the LLM call + defensive JSON parse.

:func:`_run_distiller_turn` makes ONE bounded headless ``claude-agent-sdk`` turn (the
headless-runner invocation shape: ``claude_code`` preset, ``bypassPermissions``, a
wall-clock watchdog) and raises when ``claude`` is unavailable or the turn fails, so a
failure propagates and the pass is marked attempted-not-succeeded — never a fake success.

:func:`_parse_clusters` tolerates surrounding prose and drops malformed entries so one bad
element never discards a valid batch.

:func:`deterministic_cluster_key` is the idempotency anchor — sha256 over the normalized
member set, NOT the LLM's prose slug (#2723), matching the ``ConsolidatedMemory`` docstring.
Two runs that group the same members under different wording upsert to one ledger row.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import cast

from teatree.loops.dream.engine import ConsolidationExtract, DistilledCluster

_DISTILL_SYSTEM_PROMPT = (
    "You consolidate an agent's recent feedback and lessons into durable rules. "
    "Group the snippets by ROOT CAUSE. Emit ONE imperative rule per group, and a "
    "group ONLY when it cites a SPECIFIC real mistake quoted from the snippets — "
    "never invent a rule with no cited mistake."
)

_DISTILL_PROMPT_TEMPLATE = (
    "Consolidate the following weighted snippets into root-cause clusters.\n\n"
    "Return ONLY a JSON array. Each element is an object with keys: "
    "rule (one imperative sentence), "
    "source_files (the snippet paths the rule cites — copy them verbatim), "
    "is_binding (true when a source is a BINDING/user-correction), "
    "verified_citation (a VERBATIM substring copied from one of the cited "
    "snippets — the specific real mistake the rule would have prevented; do NOT "
    "paraphrase, the quote must appear word-for-word in the snippet), "
    "durable_destination (a suggested home). Do NOT emit a cluster_key — the "
    "system derives it deterministically from source_files.\n\n"
    "Emit an element ONLY when verified_citation is a real quote present in a "
    "cited snippet below. If nothing meets the bar, return [].\n\n"
    "Snippets:\n{snippets}"
)

_DISTILL_WATCHDOG_SECONDS = 5 * 60
_DISTILL_MODEL = "claude-haiku-4-5"
#: ``cluster_key`` is NO LONGER required from the LLM — it is derived deterministically
#: from the member set (#2723), matching the ``ConsolidatedMemory`` docstring's "sha256
#: over the normalized member identities". A reworded slug for the same root cause used
#: to fork a duplicate row; the deterministic key upserts instead.
_REQUIRED_CLUSTER_KEYS = ("rule", "source_files", "is_binding", "verified_citation")


def deterministic_cluster_key(source_files: Sequence[str]) -> str:
    """The idempotency anchor: sha256 over the normalized, sorted member identities.

    The cluster's identity is its MEMBER SET, not the LLM's prose: two distiller runs
    that group the same members under different slugs derive the SAME key and upsert to
    one row (member order does not matter — the paths are normalized + sorted first).
    A blank/whitespace-only path is dropped before hashing. This matches the model
    docstring that already claims a sha256 ``cluster_key`` (#2723).
    """
    members = sorted({path.strip() for path in source_files if path.strip()})
    return hashlib.sha256("\n".join(members).encode("utf-8")).hexdigest()


def sdk_distiller(extract: ConsolidationExtract) -> list[DistilledCluster]:
    """The real distiller: one bounded headless SDK call, parsed defensively.

    An empty extract short-circuits without an LLM call. Otherwise one bounded
    turn through :func:`_run_distiller_turn` produces JSON, which is parsed into
    clusters; malformed or partial JSON yields no clusters rather than a crash.
    An SDK failure propagates so the command marks the pass attempted-not-
    succeeded (staleness keeps firing) — never laundered into a fake success.
    """
    if not extract.snippets:
        return []
    raw = _run_distiller_turn(extract)
    return _parse_clusters(raw)


def _render_snippets(extract: ConsolidationExtract) -> str:
    return "\n\n".join(
        f"--- {snippet.path} (weight={snippet.weight}) ---\n{snippet.text}" for snippet in extract.snippets
    )


def _run_distiller_turn(extract: ConsolidationExtract) -> str:
    """Run one bounded headless ``claude-agent-sdk`` turn, returning its text.

    Reuses the headless-runner invocation shape (the ``claude_code`` preset,
    ``bypassPermissions``, a wall-clock watchdog via :func:`asyncio.wait_for`)
    for a single no-tool turn — the extract is already bounded, so the model
    only transforms text to JSON. Raises when ``claude`` is unavailable or the
    turn fails, so the caller never reports a fake success.
    """
    import asyncio  # noqa: PLC0415
    import shutil  # noqa: PLC0415

    if shutil.which("claude") is None:
        msg = "claude is not installed — the dream distiller cannot run"
        raise RuntimeError(msg)
    prompt = _DISTILL_PROMPT_TEMPLATE.format(snippets=_render_snippets(extract))
    return asyncio.run(_collect_turn(prompt))


async def _collect_turn(prompt: str) -> str:
    import asyncio  # noqa: PLC0415

    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, TextBlock  # noqa: PLC0415
    from claude_agent_sdk.types import SystemPromptPreset  # noqa: PLC0415

    options = ClaudeAgentOptions(
        system_prompt=SystemPromptPreset(type="preset", preset="claude_code", append=_DISTILL_SYSTEM_PROMPT),
        model=_DISTILL_MODEL,
        permission_mode="bypassPermissions",
        max_turns=1,
        allowed_tools=[],
    )
    parts: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)

        async def _drain() -> list[object]:
            return [message async for message in client.receive_response()]

        for message in await asyncio.wait_for(_drain(), timeout=_DISTILL_WATCHDOG_SECONDS):
            if isinstance(message, AssistantMessage):
                parts.extend(block.text for block in message.content if isinstance(block, TextBlock))
    return "\n".join(parts)


def _parse_clusters(raw: str) -> list[DistilledCluster]:
    """Parse the distiller's JSON array into clusters, dropping malformed entries.

    Tolerates surrounding prose by scanning for the first ``[`` … matching
    ``]``. An entry missing a required key is skipped (not fatal), so one bad
    element never discards a whole valid batch.
    """
    payload = _extract_json_array(raw)
    if payload is None:
        return []
    clusters: list[DistilledCluster] = []
    for entry in payload:
        cluster = _coerce_cluster(entry)
        if cluster is not None:
            clusters.append(cluster)
    return clusters


def _extract_json_array(raw: str) -> list[object] | None:
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _coerce_cluster(entry: object) -> DistilledCluster | None:
    if not isinstance(entry, Mapping):
        return None
    fields = cast("Mapping[str, object]", entry)
    if any(key not in fields for key in _REQUIRED_CLUSTER_KEYS):
        return None
    source_files = fields["source_files"]
    if not isinstance(source_files, list):
        return None
    paths = [str(path) for path in source_files]
    return DistilledCluster(
        cluster_key=deterministic_cluster_key(paths),
        rule=str(fields["rule"]),
        source_files=paths,
        is_binding=bool(fields["is_binding"]),
        verified_citation=str(fields["verified_citation"]),
        durable_destination=str(fields.get("durable_destination", "")),
    )


__all__ = ["deterministic_cluster_key", "sdk_distiller"]
