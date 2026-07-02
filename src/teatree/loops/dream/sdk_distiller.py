"""The real LLM distiller: extract → root-cause clusters via one headless SDK call.

This is the only place the dream pass touches an LLM. :func:`sdk_distill` is the default
:class:`~teatree.loops.dream.engine.Distiller` the engine injects; tests pass a fake so the
engine — and every phase around it — runs with no LLM. The concern split out of
:mod:`teatree.loops.dream.engine` (#2723) is the LLM call + defensive JSON parse.

:func:`_run_distiller_turn` makes ONE bounded headless ``claude-agent-sdk`` turn (the
headless-runner invocation shape: ``claude_code`` preset, ``bypassPermissions``, a
wall-clock watchdog) and raises when ``claude`` is unavailable or the turn fails, so a
failure propagates and the pass is marked attempted-not-succeeded — never a fake success.
The watchdog (:func:`asyncio.timeout`) bounds the WHOLE turn — the ``claude`` connect, the
query, AND the response drain — so a stuck subprocess connect can never hang the dream pass
forever (the prior watchdog wrapped only the response drain, leaving connect/query
unbounded: a stalled ``claude`` spawn hung the pass with no rows, no marker, no output).

:func:`_extract_json_array` finds the model's top-level JSON array tolerating bracket-heavy
prose around it (a balanced-bracket scan, not the prior greedy first-``[`` … last-``]``
span that swallowed prose brackets and silently yielded 0 — #2847). :func:`sdk_distill`
classifies an empty return into a :class:`~teatree.loops.dream.engine.DistillEmptyReason`
so a genuine no-consolidation is told from a broken parse; :func:`sdk_distiller` is the
clusters-only convenience for callers that do not need that diagnostic.

:func:`deterministic_cluster_key` is the idempotency anchor — sha256 over the normalized
member set, NOT the LLM's prose slug (#2723), matching the ``ConsolidatedMemory`` docstring.
Two runs that group the same members under different wording upsert to one ledger row.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import cast

from teatree.loops.dream.engine import ConsolidationExtract, DistilledCluster, DistillEmptyReason, DistillResult
from teatree.loops.dream.json_scan import first_object_bearing_array

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

#: A fenced ```json … ``` block the model may wrap its array in. Tried after a direct
#: decode and before the balanced-bracket scan in :func:`_extract_json_array`.
_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


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


def sdk_distill(extract: ConsolidationExtract) -> DistillResult:
    """The real distiller: one bounded headless SDK call, parsed defensively (#2847).

    An empty extract short-circuits without an LLM call. Otherwise one bounded
    turn through :func:`_run_distiller_turn` produces JSON, which is parsed into
    clusters; malformed or partial JSON yields no clusters rather than a crash.
    When 0 clusters result, the :class:`~teatree.loops.dream.engine.DistillResult`
    carries WHY (empty raw / unparsable / all-entries-dropped / genuine empty
    array) so the operator can tell a healthy no-consolidation from a broken parse.
    An SDK failure propagates so the command marks the pass attempted-not-
    succeeded (staleness keeps firing) — never laundered into a fake success.
    """
    if not extract.snippets:
        return DistillResult(clusters=[], empty_reason=DistillEmptyReason.NOTHING_TO_CONSOLIDATE)
    raw = _run_distiller_turn(extract)
    return _parse_distill_result(raw)


def sdk_distiller(extract: ConsolidationExtract) -> list[DistilledCluster]:
    """Clusters-only distiller for callers that do not need the empty-reason diagnostic."""
    return sdk_distill(extract).clusters


def _render_snippets(extract: ConsolidationExtract) -> str:
    return "\n\n".join(
        f"--- {snippet.path} (weight={snippet.weight}) ---\n{snippet.text}" for snippet in extract.snippets
    )


def _run_distiller_turn(extract: ConsolidationExtract) -> str:
    """Run one bounded headless ``claude-agent-sdk`` turn, returning its text.

    Reuses the headless-runner invocation shape (the ``claude_code`` preset,
    ``bypassPermissions``, a wall-clock watchdog via :func:`asyncio.timeout`)
    for a single no-tool turn — the extract is already bounded, so the model
    only transforms text to JSON. Raises when ``claude`` is unavailable or the
    turn fails (including :class:`TimeoutError` when the turn exceeds the
    watchdog), so the caller never reports a fake success and never hangs.
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
    # Bound the ENTIRE turn — connect (``__aenter__`` spawns the ``claude``
    # subprocess), query, AND the response drain — under one watchdog. Wrapping
    # only the drain (the prior shape) left connect/query unbounded, so a stalled
    # ``claude`` connect hung the dream pass forever (no rows, no marker, no
    # output) instead of failing loud; ``asyncio.timeout`` raises ``TimeoutError``
    # on expiry and the ``async with`` tears the subprocess down on unwind.
    async with asyncio.timeout(_DISTILL_WATCHDOG_SECONDS), ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                parts.extend(block.text for block in message.content if isinstance(block, TextBlock))
    return "\n".join(parts)


def _parse_distill_result(raw: str) -> DistillResult:
    """Parse the distiller's reply into clusters AND classify an empty result (#2847).

    Drops malformed entries so one bad element never discards a valid batch. When the
    result is empty the reason distinguishes a broken parse (empty/whitespace raw,
    unparsable raw, or an array whose every entry was malformed) from a genuine empty
    array (the model found nothing to consolidate — healthy).
    """
    if not raw.strip():
        return DistillResult(clusters=[], empty_reason=DistillEmptyReason.EMPTY_RAW)
    payload = _extract_json_array(raw)
    if payload is None:
        return DistillResult(clusters=[], empty_reason=DistillEmptyReason.UNPARSABLE)
    clusters: list[DistilledCluster] = []
    for entry in payload:
        cluster = _coerce_cluster(entry)
        if cluster is not None:
            clusters.append(cluster)
    if clusters:
        return DistillResult(clusters=clusters, empty_reason=None)
    reason = DistillEmptyReason.NOTHING_TO_CONSOLIDATE if not payload else DistillEmptyReason.ALL_ENTRIES_DROPPED
    return DistillResult(clusters=[], empty_reason=reason)


def _extract_json_array(raw: str) -> list[object] | None:
    """Find the model's top-level JSON array, tolerating bracketed prose around it.

    Three tiers, first hit wins: (1) the stripped reply IS a JSON array; (2) a fenced
    ```json code block holds one; (3) a balanced-bracket scan
    (:func:`~teatree.loops.dream.json_scan.first_object_bearing_array`) returns the
    first top-level ``[`` span carrying an object, else the first decodable list. The
    scan — not the prior greedy first-``[`` … last-``]`` span — makes bracket-heavy
    prose (markdown links, ``#N`` refs, regex classes) around the array safe: a prose
    ``[…]`` that is not valid JSON is skipped (#2847), and a prose scalar/empty array
    appearing BEFORE the real cluster array no longer wins over it (#2861). A genuine
    empty array as the whole reply still resolves via tier 1, so the healthy 0-cluster
    path is untouched.
    """
    direct = _loads_array(raw.strip())
    if direct is not None:
        return direct
    for match in _JSON_FENCE.finditer(raw):
        fenced = _loads_array(match.group(1).strip())
        if fenced is not None:
            return fenced
    return first_object_bearing_array(raw)


def _loads_array(text: str) -> list[object] | None:
    try:
        parsed = json.loads(text)
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


__all__ = ["deterministic_cluster_key", "sdk_distill", "sdk_distiller"]
