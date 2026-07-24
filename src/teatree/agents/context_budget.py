"""Byte budget for the headless system-context append — the E2BIG spawn guard.

The claude-agent-sdk passes the whole assembled system context as ONE
``--append-system-prompt`` argv element (its subprocess transport). Linux caps a
single argv element at ``MAX_ARG_STRLEN`` = 128 KiB, so an oversized append makes
the ``claude`` child die at spawn with ``OSError: [Errno 7] Argument list too
long``. :data:`MAX_APPEND_BYTES` bounds the append well under that limit, leaving
headroom for the rest of argv; :func:`enforce_budget` truncates the largest
budgetable blocks first and leaves a pointer marker so the agent knows context
was elided rather than silently dropped.
"""

from collections.abc import Iterable

# 96 KiB — comfortably below the 128 KiB kernel per-argv-element limit, leaving
# ~32 KiB of headroom for the preset prefix and the rest of the spawn argv.
MAX_APPEND_BYTES = 96 * 1024


def _marker(dropped_bytes: int, where: str) -> str:
    return f"\n[…truncated {dropped_bytes} bytes; see {where}]"


def _truncate_block(block: str, keep_bytes: int, *, where: str) -> str:
    """Return *block* shortened to at most *keep_bytes* UTF-8 bytes plus a pointer marker.

    The marker's byte count is sized against the worst case (the whole block
    dropped) so a shorter actual drop only shrinks the marker — the result never
    exceeds *keep_bytes*. The kept prefix is decoded with ``errors="ignore"`` so a
    byte-slice never splits a multibyte codepoint.
    """
    encoded = block.encode()
    if keep_bytes >= len(encoded):
        return block
    worst_case_marker = _marker(len(encoded), where)
    content_budget = max(0, keep_bytes - len(worst_case_marker.encode()))
    kept = encoded[:content_budget].decode(errors="ignore")
    dropped = len(encoded) - len(kept.encode())
    return f"{kept}{_marker(dropped, where)}"


def enforce_budget(text: str, blocks: Iterable[tuple[str, str]], *, max_bytes: int = MAX_APPEND_BYTES) -> str:
    """Bound *text* to *max_bytes*, truncating *blocks* in the given priority order.

    *blocks* is an ordered iterable of ``(block_text, where)`` pairs — each
    ``block_text`` is an exact substring of *text*, and ``where`` names where the
    elided content still lives (the pointer the marker cites). Earlier blocks are
    truncated first, so pass them least-load-bearing first. Returns *text*
    unchanged (byte-identical) when it already fits, so a normal-sized context is
    never rewritten.
    """
    overage = len(text.encode()) - max_bytes
    if overage <= 0:
        return text
    for block, where in blocks:
        if overage <= 0 or not block:
            continue
        block_bytes = len(block.encode())
        truncated = _truncate_block(block, block_bytes - overage, where=where)
        reclaimed = block_bytes - len(truncated.encode())
        if reclaimed <= 0:
            continue
        text = text.replace(block, truncated, 1)
        overage -= reclaimed
    return text
