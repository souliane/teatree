"""UserPromptSubmit: cold-tier memory recall injection (#2746).

When the user's prompt is topically relevant to a memory rule that PR1 (#2723)
archived out of the session-loaded hot ``MEMORY.md`` into the cold tier, this thin
handler surfaces it for that one turn. On ``UserPromptSubmit``, stdout IS
``additionalContext`` (the same mechanism ``handle_user_prompt_submit`` uses), so a
non-empty recall block printed here is injected into the agent's context.

The handler is a thin shell: it resolves the project memory dir, defers all scoring to
the DB-free pure core ``teatree.loops.dream.recall`` (imported with ``src/`` on
``sys.path`` but NO ``django.setup()`` — the core is stdlib-only), and prints the
rendered block. Crash-proof and fail-silent: a missing transcript dir, a missing cold
index, the kill-switch off, or any error all inject NOTHING (silent degrade).

NEVER-LOCKOUT: ``[teatree] memory_recall_enabled = false`` (``t3 <overlay> gate
memory-recall disable``) disables the injector; it ships default-ON.
"""

import contextlib
import sys
from pathlib import Path

# Alias both identities so the handler the router registers and a test patching a
# helper here operate on ONE module object (mirrors the other hook leaves).
sys.modules.setdefault("memory_recall", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.memory_recall", sys.modules[__name__])

# Put this script's own dir on sys.path so the bare ``teatree_settings`` import
# resolves whether this runs as the live hook or is imported as
# ``hooks.scripts.memory_recall`` in a subprocess/test.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


def _memory_recall_enabled() -> bool:
    """Whether the cold-tier recall injector is enabled (default True).

    Fails OPEN to enabled on a missing/broken config; an explicit ``false``
    (``[teatree] memory_recall_enabled = false``, flipped by
    ``t3 <overlay> gate memory-recall disable``) is the one-line kill-switch.
    """
    from teatree_settings import teatree_bool_setting  # noqa: PLC0415

    return teatree_bool_setting("memory_recall_enabled", default=True)


def _load_recall():  # noqa: ANN202 — the imported module has no stable type to annotate.
    """Import the pure recall core, bootstrapping the sibling ``src/`` onto the path.

    The hook runs in the user's session shell with no guarantee ``teatree`` is
    importable, so ``src/`` is added to ``sys.path`` first (mirroring the other
    leaves). The core is DB-free and stdlib-only at the top level, so NO
    ``django.setup()`` is needed. Returns the module, or ``None`` on any import
    failure — the caller then injects nothing (a cold env must never traceback).
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        from teatree.loops.dream import recall  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — a cold env without teatree injects nothing, never tracebacks.
        return None
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))
    return recall


def _project_memory_dir(data: dict, cold_index_name: str) -> Path | None:
    """Resolve the project's memory dir holding the cold index, or ``None``.

    PRIMARY: the transcript's sibling ``memory/`` dir
    (``Path(transcript_path).parent / "memory"``). FALLBACK: the
    ``~/.claude/projects/<cwd-slug>/memory`` dir, deriving the slug from ``cwd``
    (``/`` → ``-``) the way the harness names project dirs. Each candidate is
    accepted only if it actually holds the cold index file; otherwise ``None``
    (inject nothing — silent degrade).
    """
    transcript_path = data.get("transcript_path", "")
    if isinstance(transcript_path, str) and transcript_path:
        candidate = Path(transcript_path).parent / "memory"
        if (candidate / cold_index_name).is_file():
            return candidate
    cwd = data.get("cwd", "")
    if isinstance(cwd, str) and cwd:
        candidate = Path.home() / ".claude" / "projects" / cwd.replace("/", "-") / "memory"
        if (candidate / cold_index_name).is_file():
            return candidate
    return None


def handle_recall_cold_memory(data: dict) -> None:
    """Inject the recall block for the prompt's relevant cold-tier rules (if any).

    Fail-silent on every path: a disabled kill-switch, an unimportable core, no
    resolvable cold-index dir, or any scoring error all inject nothing. The WHOLE
    body — the kill-switch read and the dir resolution included — is wrapped, so a
    misbehaving config reader or a path resolver on a hostile payload can never
    traceback out of this UserPromptSubmit leaf.
    """
    if not isinstance(data, dict):
        return
    block = ""
    try:
        if not _memory_recall_enabled():
            return
        recall = _load_recall()
        if recall is None:
            return
        memory_dir = _project_memory_dir(data, recall.COLD_INDEX_NAME)
        if memory_dir is None:
            return
        hits = recall.recall_cold_memory(memory_dir, data.get("prompt", ""))
        block = recall.render_recall_block(hits)
    except Exception:  # noqa: BLE001 — UserPromptSubmit hook must be crash-proof.
        return
    if block:
        print(block)  # noqa: T201 — on UserPromptSubmit stdout IS additionalContext.
