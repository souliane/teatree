"""Balanced-scan JSON extractors that skip bracketed/braced prose (#2847, #2861).

The dream LLM-reply parse path must find the model's real JSON payload even when
the reply wraps it in prose that itself carries brackets (markdown refs, ``#N``
citations, regex classes) or braces. A greedy first-opener…last-closer span
swallows those and yields nothing; a :meth:`json.JSONDecoder.raw_decode` scan at
each top-level opener skips a prose span that is not valid JSON.

When SEVERAL balanced spans decode, a prose scalar array (``[1, 2, 3]``) or an
empty array (``[]``) can appear BEFORE the real cluster array, and a prose empty
object (``{}``) before the real synthesized object (#2861) — the greedy-scan
successors of the pre-#2850 bug. Both extractors therefore prefer the first span
carrying CONTENT (an array holding at least one object, an object holding at
least one key) and fall back to the first decodable span only when none
qualifies, so a genuinely content-free reply still resolves to a value the caller
classifies rather than swallowing a later real payload.
"""

import json
from collections.abc import Iterator, Mapping
from typing import cast

_DECODER = json.JSONDecoder()


def _top_level_spans(raw: str, opener: str) -> Iterator[object]:
    """Yield each TOP-LEVEL JSON value anchored at *opener*, left to right.

    A successful ``raw_decode`` jumps the cursor PAST the decoded value's end, so a
    nested opener inside an already-decoded span is never re-scanned — the caller
    sees only top-level payloads, not the objects nested inside one.
    """
    index = raw.find(opener)
    while index != -1:
        try:
            parsed, end = _DECODER.raw_decode(raw, index)
        except json.JSONDecodeError:
            index = raw.find(opener, index + 1)
            continue
        yield parsed
        index = raw.find(opener, end)


def first_object_bearing_array(raw: str) -> list[object] | None:
    """The first top-level JSON array carrying an object, else the first array (#2861).

    Prefers the first ``[``-anchored span that decodes to a list containing at
    least one JSON object — the real cluster array — over an earlier prose scalar
    or empty array. Falls back to the first decodable list when none carries an
    object, so an all-scalar reply still yields a payload the caller can classify
    as all-entries-dropped rather than mis-reading a later array.
    """
    fallback: list[object] | None = None
    for parsed in _top_level_spans(raw, "["):
        array = cast("list[object]", parsed)
        if any(isinstance(entry, Mapping) for entry in array):
            return array
        if fallback is None:
            fallback = array
    return fallback


def first_content_bearing_object(raw: str) -> Mapping[str, object] | None:
    """The first top-level non-empty JSON object, else the first object (#2861).

    The object analogue of :func:`first_object_bearing_array`: prefers the first
    ``{``-anchored span that decodes to a NON-EMPTY mapping — the real synthesized
    scenario — over an earlier prose empty object, and falls back to the first
    decodable object when none carries a key.
    """
    fallback: Mapping[str, object] | None = None
    for parsed in _top_level_spans(raw, "{"):
        obj = cast("Mapping[str, object]", parsed)
        if obj:
            return obj
        if fallback is None:
            fallback = obj
    return fallback


__all__ = ["first_content_bearing_object", "first_object_bearing_array"]
