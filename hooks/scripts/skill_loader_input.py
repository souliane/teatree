"""Building the input the skill-selection resolver reads, and the ambient-context strip.

Moved out of the shrink-only ``hook_router`` god-module (``hooks/CLAUDE.md`` § "Adding a
gate"): the SessionStart and UserPromptSubmit skill paths both need it, and a second copy
would be a second answer to "what does the selector see".

The strip is load-bearing, not cosmetic — see :func:`strip_ambient_context`.

Cold-import safe: stdlib only at module top; the router helpers are back-imported lazily.
"""

import os
import re
import sys
from pathlib import Path

# Alias both identities so a bare import (the live hook, whose dir is on ``sys.path``) and
# ``hooks.scripts.skill_loader_input`` resolve the SAME module object.
sys.modules.setdefault("skill_loader_input", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.skill_loader_input", sys.modules[__name__])


# Harness-injected ambient context — NOT task intent. The Claude Code
# harness appends ``<system-reminder>…</system-reminder>`` blocks (the
# CLAUDE.md body, the MEMORY.md index, the available-skills listing) to
# the prompt that reaches ``UserPromptSubmit``. Keyword-matching those
# blocks is the #1567 over-fire: a MEMORY.md index line naming
# ``feedback_blog_*`` keyword-matched ``\bblog\b`` → suggested
# ``ac-writing-blog-posts`` → the PreToolUse gate hard-blocked every
# Bash/Edit/Write during an unrelated autonomous loop. The hard-block
# demand set must derive from genuine task-intent text only, so these
# wrappers are stripped before the prompt is matched.
_AMBIENT_CONTEXT_RE = re.compile(
    r"<(system-reminder|command-message|command-name|command-args|local-command-stdout)\b[^>]*>"
    r".*?</\1>",
    re.DOTALL | re.IGNORECASE,
)

# The block regex is O(n²) against many UNTERMINATED open tags (a user
# pasting a large log/transcript that quotes literal ``<system-reminder>``
# open tags, or a malicious agent). ``strip_ambient_context`` runs on
# EVERY ``UserPromptSubmit`` and is net-new hot-path cost, so the input is
# capped before the regexes run — bounding the worst case well under the
# 30s ``UserPromptSubmit`` timeout (hooks/CLAUDE.md "hooks must be fast").
# Genuine task intent sits early in the prompt (the harness appends ambient
# blocks), so a 64 KiB cap never truncates intent — mirrors the 512-char
# token windows used elsewhere in this file.
_AMBIENT_STRIP_MAX_CHARS: int = 65536


def strip_ambient_context(prompt: str) -> str:
    """Remove harness-injected ambient-context blocks from *prompt*.

    Returns the prompt with every ``<system-reminder>`` / harness
    ``<command-*>`` wrapper (and its body) removed, leaving only the
    genuine task-intent text. An unterminated opening wrapper (truncated
    injection) is dropped from its tag to end-of-string so leaked ambient
    text can never reach the keyword matcher. The intent text is what the
    high-confidence hard-block demand set is built from (#1567).

    The input is capped to :data:`_AMBIENT_STRIP_MAX_CHARS` before
    matching to keep this hot-path hook fast (see the constant's note).
    """
    prompt = prompt[:_AMBIENT_STRIP_MAX_CHARS]
    stripped = _AMBIENT_CONTEXT_RE.sub(" ", prompt)
    stripped = re.sub(
        r"<(system-reminder|command-message|command-name|command-args|local-command-stdout)\b[^>]*>.*",
        " ",
        stripped,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return stripped.strip()


def build_skill_loader_input(prompt: str, session_id: str) -> dict:
    teatree_home = os.environ.get("HOME", "")
    source_root = Path(__file__).resolve().parents[2].parent

    from hooks.scripts.hook_router import _read_lines, _state_file  # noqa: PLC0415 deferred back-import

    active = _read_lines(_state_file(session_id, "active"))
    loaded = _read_lines(_state_file(session_id, "skills"))

    search_dirs = [str(source_root), f"{teatree_home}/.agents/skills", f"{teatree_home}/.claude/skills"]
    return {
        "prompt": strip_ambient_context(prompt),
        "cwd": str(Path.cwd()),
        "active_repos": active,
        "loaded_skills": loaded,
        "skill_search_dirs": [d for d in search_dirs if d],
        "supplementary_config": os.environ.get("T3_SUPPLEMENTARY_SKILLS", f"{teatree_home}/.teatree-skills.yml"),
    }


__all__ = ["build_skill_loader_input", "strip_ambient_context"]
