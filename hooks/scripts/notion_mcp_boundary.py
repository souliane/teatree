"""Hold a Notion connector write to the page boundary: read the page this session, and write only a live one (#5023).

``handle_track_notion_pages`` (``PostToolUse``) stamps a page in ``<session>.notion-fetches`` once a direct
fetch of it came back with that page's own identity. ``handle_block_notion_write_boundary`` (``PreToolUse``)
refuses a connector write whose target page is not provably the live one, or was not stamped by this
session. A sub-agent keeps its own stamps (keyed by its agent id) and fetches for itself. teatree's own
``notion_*`` MCP tools are left to their write seam, which already enforces liveness and approval.

A probe that cannot run (no token, no network, no Django) is named on stderr and the stamp check still
applies. A completed verdict other than LIVE refuses: an UNKNOWN is not a pass. A LIVE verdict is cached
briefly per session and page, and a completed write drops it.

Cold-import safe: the module top imports only stdlib and hook siblings; the Notion client and
``teatree.hooks`` load inside the handlers, after ``src/`` is on ``sys.path``.
"""

import re
import signal
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING

from hooks.scripts.django_bootstrap import bootstrap_django_registry
from hooks.scripts.hook_budget import remaining_timeout_s
from hooks.scripts.managed_repo import teatree_src_on_path
from hooks.scripts.mcp_slack_write_guard import is_teatree_mcp_tool
from hooks.scripts.state_files import append_line, read_lines

if TYPE_CHECKING:
    from teatree.backends.notion.liveness import LivenessVerdict

sys.modules.setdefault("notion_mcp_boundary", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.notion_mcp_boundary", sys.modules[__name__])

_LIVE_TTL_SECONDS = 60
_PROBE_BUDGET_SECONDS = 12.0
_LIVE_CACHE_ENTRIES = 50
_PAGE_KEYS = frozenset({"page_id"})
_BLOCK_KEYS = frozenset({"block_id"})
_REQUEST_KEYS = frozenset({"id", "page_id", "block_id", "url"})
_DASHED_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_HEX32 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
_BODY_KEYS = frozenset({"children", "content", "properties", "rich_text", "paragraph", "link_to_page", "blocks"})
_FAILED_RESPONSE_RE = re.compile(
    r'^\s*(?:error\b|\{\s*"object"\s*:\s*"error"|object_not_found\b|restricted_resource\b|validation_error\b)',
    re.IGNORECASE,
)
_ESCAPE = (
    "One-off escape: use a non-published tool-input field for `[gate-escape:notion_boundary: <reason>]`. "
    "Never put it in page or comment text."
)


def _stamp_owner(data: dict) -> str:
    session = str(data.get("session_id") or "")
    agent = str(data.get("agent_id") or "")
    return f"{session}.{agent}" if session and agent else session


def _state_path(owner: str, suffix: str) -> Path:
    from hooks.scripts.hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415 deferred back-import

    _ensure_state_dir()
    return _state_file(owner, suffix)


def _canonical(reference: str) -> str | None:
    candidate = reference.strip().rstrip("/")
    if dashed := _DASHED_UUID.search(candidate):
        return dashed.group(0).lower()
    matches = _HEX32.findall(candidate)
    if not matches:
        return None
    raw = matches[-1].lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def _response_failed(response: object, text: list[str]) -> bool:
    if isinstance(response, Mapping) and (
        response.get("is_error") or response.get("isError") or response.get("object") == "error"
    ):
        return True
    if not text:
        return True
    if isinstance(response, str):
        return bool(_FAILED_RESPONSE_RE.match(response))
    if isinstance(response, Mapping) and response.get("type") == "text" and isinstance(response.get("text"), str):
        return bool(_FAILED_RESPONSE_RE.match(response["text"]))
    blocks = response.get("content", []) if isinstance(response, Mapping) else response
    if isinstance(blocks, list):
        return any(
            _FAILED_RESPONSE_RE.match(block["text"])
            for block in blocks
            if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str)
        )
    return False


def _write_targets(tool_input: object) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    pending = [tool_input]
    while pending:
        item = pending.pop()
        if isinstance(item, Mapping):
            for key, child in item.items():
                if key in _PAGE_KEYS | _BLOCK_KEYS and isinstance(child, str):
                    targets.append((child, "page" if key in _PAGE_KEYS else "block"))
                elif key == "page_or_database_ids" and isinstance(child, list):
                    targets.extend((reference, "page") for reference in child if isinstance(reference, str))
                elif key not in _BODY_KEYS:
                    pending.append(child)
        elif isinstance(item, list):
            pending.extend(item)
    return targets


def _stamp_fetch(owner: str, data: dict) -> None:
    from teatree.hooks.mcp_publish import text_leaves, values_under_keys  # noqa: PLC0415 — after src bootstrap

    response = data.get("tool_response")
    text = text_leaves(response)
    if _response_failed(response, text):
        return
    returned = "".join(text).lower().replace("-", "")
    requested = (_canonical(reference) for reference in values_under_keys(data.get("tool_input"), _REQUEST_KEYS))
    for page_id in dict.fromkeys(page_id for page_id in requested if page_id):
        if page_id.replace("-", "") in returned:
            _record_fetch(owner, page_id)


def _record_fetch(owner: str, page_id: str) -> None:
    path = _state_path(owner, "notion-fetches")
    if page_id not in read_lines(path):
        append_line(path, page_id)
    path.touch()
    if page_id not in read_lines(path):
        sys.stderr.write(
            f"[teatree] NOTE: could not record that this session read Notion page {page_id}; "
            "a later write to it will be refused.\n"
        )


def _live_cache(owner: str) -> dict[str, float]:
    entries: dict[str, float] = {}
    for line in read_lines(_state_path(owner, "notion-live")):
        page_id, _, expiry = line.partition("\t")
        if expiry.replace(".", "", 1).isdigit():
            entries[page_id] = float(expiry)
    return entries


def _store_live_cache(owner: str, entries: dict[str, float]) -> None:
    now = time.time()
    kept = [(page_id, expiry) for page_id, expiry in entries.items() if expiry > now][-_LIVE_CACHE_ENTRIES:]
    _state_path(owner, "notion-live").write_text(
        "".join(f"{page_id}\t{expiry}\n" for page_id, expiry in kept), encoding="utf-8"
    )


def _drop_live(owner: str, page_ids: list[str]) -> None:
    cache = _live_cache(owner)
    if any(page_id in cache for page_id in page_ids):
        _store_live_cache(owner, {page_id: expiry for page_id, expiry in cache.items() if page_id not in page_ids})


class _ProbeOutOfTimeError(BaseException):
    """Raised by SIGALRM; a BaseException so a client's own `except Exception` retry cannot swallow it."""


def _probe_out_of_time(_signum: int, _frame: FrameType | None) -> None:
    raise _ProbeOutOfTimeError


def _skipped(page_id: str, why: str) -> None:
    sys.stderr.write(
        f"[teatree] NOTE: Notion liveness of {page_id} was not checked ({why}). "
        "The read-this-session check still applies.\n"
    )


def _probe(page_id: str) -> "LivenessVerdict | None":
    budget = remaining_timeout_s(_PROBE_BUDGET_SECONDS)
    if budget is None:
        _skipped(page_id, "no time left in the hook budget")
        return None
    previous = signal.signal(signal.SIGALRM, _probe_out_of_time)
    signal.setitimer(signal.ITIMER_REAL, budget)
    try:
        if not bootstrap_django_registry():
            _skipped(page_id, "Django is unavailable in the hook interpreter")
            return None
        from teatree.backends.notion.credentials import build_notion_client  # noqa: PLC0415 — after src bootstrap

        return build_notion_client().page_liveness(page_id)
    except _ProbeOutOfTimeError:
        _skipped(page_id, f"the probe ran past its {budget:.0f}s share of the hook's time")
    except Exception as exc:  # noqa: BLE001 — a probe that cannot run is named and never decides the verdict
        _skipped(page_id, f"{type(exc).__name__}: {str(exc).partition(chr(10))[0]}")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous or signal.SIG_DFL)
    return None


def _not_live_reason(owner: str, page_id: str) -> str | None:
    cache = _live_cache(owner)
    if cache.get(page_id, 0.0) > time.time():
        return None
    verdict = _probe(page_id)
    if verdict is None:
        return None
    if verdict.readable:
        _store_live_cache(owner, {**cache, page_id: time.time() + _LIVE_TTL_SECONDS})
        return None
    return (
        f"BLOCKED: this Notion write targets {verdict.headline(page_id)}. {verdict.recovery()} "
        f"To inspect it, run `t3 notion audit-fetch {page_id} --reason '<why>'`: that is an audit read, it "
        f"does not authorize this write. {_ESCAPE}"
    )


def handle_block_notion_write_boundary(data: dict) -> bool:
    """Refuse a Notion connector write to a page that is not live, or that this session did not read."""
    tool_name = str(data.get("tool_name") or "")
    owner = _stamp_owner(data)
    if not owner or "notion" not in tool_name.lower() or is_teatree_mcp_tool(tool_name):
        return False
    with teatree_src_on_path():
        from teatree.hooks.mcp_publish import is_notion_mcp_write  # noqa: PLC0415 — after bootstrap

        if not is_notion_mcp_write(tool_name):
            return False
        tool_input = data.get("tool_input")
        targets = _write_targets(tool_input)
        reason = _boundary_refusal(owner, targets)
    if reason is None:
        return False
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    return _fail_open_or_deny(data, reason, gate_id="notion_boundary")


def _boundary_refusal(owner: str, targets: list[tuple[str, str]]) -> str | None:
    resolved: list[tuple[str, str]] = []
    for reference, noun in targets:
        page_id = _canonical(reference)
        if page_id is None:
            return (
                f"BLOCKED: this Notion write names {reference!r}, which is not a Notion {noun} id or URL, so the "
                f"page boundary cannot be checked. Name the {noun} by its id. {_ESCAPE}"
            )
        resolved.append((page_id, noun))
    read = set(read_lines(_state_path(owner, "notion-fetches")))
    for page_id, noun in resolved:
        if page_id not in read:
            return (
                f"BLOCKED: this Notion write targets {noun} {page_id}, which was not read in this session. Fetch it "
                "first (notion-fetch) so the edit is made against its current content; a sub-agent keeps its own "
                f"reads and must fetch it itself. {_ESCAPE}"
            )
    for page_id in dict.fromkeys(page_id for page_id, noun in resolved if noun == "page"):
        if reason := _not_live_reason(owner, page_id):
            return reason
    return None


def handle_track_notion_pages(data: dict) -> None:
    """Stamp a page a direct fetch returned, and drop the cached live verdict of a page a write just changed."""
    tool_name = str(data.get("tool_name") or "")
    owner = _stamp_owner(data)
    if not owner or "notion" not in tool_name.lower() or is_teatree_mcp_tool(tool_name):
        return
    with teatree_src_on_path():
        from teatree.hooks.mcp_publish import (  # noqa: PLC0415 — after src bootstrap
            is_notion_mcp_fetch,
            is_notion_mcp_write,
        )

        if is_notion_mcp_fetch(tool_name):
            _stamp_fetch(owner, data)
        elif is_notion_mcp_write(tool_name):
            written = [reference for reference, _noun in _write_targets(data.get("tool_input"))]
            _drop_live(owner, [page_id for page_id in map(_canonical, written) if page_id])
