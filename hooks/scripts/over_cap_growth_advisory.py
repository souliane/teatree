"""PostToolUse: surface the shrink ratchet's refusal at edit time, not commit time (#2663).

``check_module_health`` grandfathers an over-cap module and lets it only SHRINK, so
adding two lines to one is refused — but only once the commit runs. An agent therefore
discovers "extract offsetting code first" AFTER writing the code, and pays for the
extraction as rework. This advisory measures the SAME growth the ratchet will refuse,
the moment the write lands, and names how many LOC must come out.

It rides ``PostToolUse`` rather than ``PreToolUse`` for two reasons. The number is REAL
there — the file on disk is the post-edit body, so the net delta against ``HEAD`` is the
one the ratchet will compute, not a guess about an edit that has not happened. And its
``additionalContext`` cannot collide with a second stdout payload on the same call: the
``PreToolUse`` chain's turn-budget nudge writes a JSON object on any tool call, whereas
the only other ``PostToolUse`` writer (``handle_read_dedup``) fires exclusively on
``Read``, which this advisory never handles. Two payloads on one call would not parse,
costing both. ``tests/test_over_cap_growth_advisory.py`` pins that exclusivity.

WARN-only by construction: ``PostToolUse`` has no deny, the handler returns ``None`` on
every path, and ``tests/test_over_cap_growth_advisory.py`` pins by source scan that no
deny helper is reachable from here. The enforcement stays where it belongs — the commit
ratchet — and this is only the earlier word about it.

Precision-biased, inheriting the posture of the resolver it consumes: a Bash target that
cannot be pinned statically is passed over rather than guessed at. A false nag on the
edit hot path costs more than a miss the ratchet catches anyway.

Cold-import safe: the live hook is a bare ``python3`` subprocess with no guarantee
``teatree`` is importable, so the module top imports only stdlib and dependency-free
hook siblings; both teatree leaves are imported lazily inside the ``src/`` bootstrap.
"""

import json
import subprocess  # noqa: S404 — stdlib subprocess for the trusted internal `git` reads
import sys
from pathlib import Path
from typing import Final

from hooks.scripts.managed_repo import teatree_src_on_path as _teatree_src_on_path

# Alias both identities so the handler the router registers and a test patching a
# helper here operate on ONE module object.
sys.modules.setdefault("over_cap_growth_advisory", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.over_cap_growth_advisory", sys.modules[__name__])

_WRITE_TOOLS: Final[frozenset[str]] = frozenset({"Edit", "Write"})
_GIT_TIMEOUT_S: Final[int] = 5
_STATE_SUFFIX: Final[str] = "over-cap-advised"

_ADVISORY: Final[str] = (
    "[module-health] `{path}` is now {message}\n"
    "The commit ratchet refuses this growth — extract BEFORE adding, not after.\n"
)


def handle_over_cap_growth_advisory(data: dict) -> None:
    """Name the ratchet's pending refusal once per session per grown module.

    Returns ``None`` on every path — there is no deny in this module. Catches its own
    exceptions rather than leaning on the router's swallow, which writes a stderr
    traceback that would read as the advisory speaking.
    """
    try:
        _advise(data)
    except Exception as exc:  # noqa: BLE001 — crash-proof: a broken advisory must never disturb the write.
        print(f"[over-cap-advisory] skipped ({exc})", file=sys.stderr)  # noqa: T201 — hook stderr is this module's logging channel


def _advise(data: dict) -> None:
    session_id = data.get("session_id", "")
    if not session_id:
        return
    for path in _written_paths(data):
        growth = _pending_refusal(path)
        if growth is not None and _claim_first_mention(session_id, growth.path):
            _emit(growth)


def _written_paths(data: dict) -> list[Path]:
    """Every ``.py`` path this tool call wrote, as far as it can be pinned statically."""
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return []
    if tool_name in _WRITE_TOOLS:
        candidates = [str(tool_input.get("file_path", ""))]
    elif tool_name == "Bash":
        candidates = _bash_targets(str(tool_input.get("command", "")), str(data.get("cwd", "")))
    else:
        return []
    return [Path(c) for c in candidates if c.endswith(".py")]


def _module_health():  # noqa: ANN202 — returns a lazily-imported module; annotating would pull it to module scope
    """The packaged module-health leaf — ONE deferred import site for the whole advisory."""
    with _teatree_src_on_path():
        from teatree.hooks.portable import check_module_health  # noqa: PLC0415 — deferred: cold-hook import

        return check_module_health


def _bash_targets(command: str, cwd: str) -> list[str]:
    """The shell command's resolvable write targets — an unpinnable one is dropped."""
    if not command:
        return []
    with _teatree_src_on_path():
        from teatree.hooks.write_targets import bash_write_targets  # noqa: PLC0415 — deferred: cold-hook import

        base = Path(cwd) if cwd else None
        return [str(p) for p in bash_write_targets(command).resolved_paths(base)]


def _pending_refusal(path: Path):  # noqa: ANN202 — returns a lazily-imported type; annotating would pull it to module scope
    """The ``OverCapGrowth`` the ratchet will refuse for *path*, or ``None``."""
    root = _repo_root(path)
    if root is None:
        return None
    try:
        rel = path.resolve().relative_to(root).as_posix()
    except ValueError:
        return None
    baseline = _blob_at_head(root, rel)
    if baseline is None:
        return None
    return _module_health().over_cap_growth(rel, source=path.read_text(encoding="utf-8"), baseline_source=baseline)


def _repo_root(path: Path) -> Path | None:
    """The repo enclosing *path*, found by walking up to a ``.git`` (file or dir)."""
    try:
        start = path.resolve().parent
    except (OSError, RuntimeError):
        return None
    return next((c for c in (start, *start.parents) if (c / ".git").exists()), None)


def _blob_at_head(root: Path, rel: str) -> str | None:
    """*rel*'s committed body, or ``None`` when ``HEAD`` does not carry it.

    A path ``HEAD`` does not carry has no grandfathered baseline to shrink back to, so
    it is the ratchet's OTHER branch (split by concern) and not this advisory's business.
    """
    result = subprocess.run(  # noqa: S603 — trusted internal subprocess; fixed argv, no shell
        ["git", "-C", str(root), "--no-optional-locks", "show", f"HEAD:{rel}"],  # noqa: S607 — trusted internal git invocation with a fixed argv
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _ledger_path(session_id: str) -> Path:
    """The session's already-advised ledger, in the router's swept state dir."""
    from hooks.scripts.hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415 — call-time back-import

    _ensure_state_dir()
    return _state_file(session_id, _STATE_SUFFIX)


def _claim_first_mention(session_id: str, rel: str) -> bool:
    """True the FIRST time *rel* is advised in *session_id* — the once-per-file latch."""
    ledger = _ledger_path(session_id)
    try:
        seen = ledger.read_text(encoding="utf-8").splitlines() if ledger.is_file() else []
        if rel in seen:
            return False
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(f"{rel}\n")
    except OSError:
        return False
    return True


def _emit(growth) -> None:  # noqa: ANN001 — the lazily-imported OverCapGrowth, see _pending_refusal
    advisory = _ADVISORY.format(path=growth.path, message=_module_health().over_cap_growth_message(growth))
    print(advisory, file=sys.stderr, end="")  # noqa: T201 — hook stderr is this module's logging channel
    print(  # noqa: T201 — hook writes its protocol output to stdout
        json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": advisory}})
    )
