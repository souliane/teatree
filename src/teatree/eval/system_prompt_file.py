"""Pass a clean-room system prompt to the CLI by file, never by argv.

A whole-skill system prompt is hundreds of KB. The SDK renders a plain-string
``system_prompt`` as a single ``--system-prompt <text>`` argv argument, which
blows ``ARG_MAX`` (``[Errno 7] Argument list too long``, E2BIG) the moment a
skill grows past the OS limit — failing the metered eval lane before any
scenario runs. Spilling the prompt to a file and pointing the CLI at it with
``--system-prompt-file <path>`` keeps the argv bounded regardless of skill size.
"""

from pathlib import Path

from claude_agent_sdk.types import SystemPromptFile

#: Filename for the system-prompt spilled into the isolated ``cwd``. Dot-prefixed
#: so it is not a natural workspace file the scenario would glob/read; it dies
#: with the ``isolated_claude_env`` ``TemporaryDirectory`` that brackets the spawn.
_SYSTEM_PROMPT_FILENAME = ".t3-eval-system-prompt.txt"


def spill_system_prompt(system_prompt: str, cwd: str) -> SystemPromptFile:
    """Write *system_prompt* into *cwd* and return the SDK ``--system-prompt-file`` ref.

    *cwd* is the ``isolated_claude_env`` temp dir that outlives the ``query``
    spawn, so the file is cleaned up with it — no separate lifetime to manage.
    """
    path = Path(cwd) / _SYSTEM_PROMPT_FILENAME
    path.write_text(system_prompt, encoding="utf-8")
    return {"type": "file", "path": str(path)}


def resolve_system_prompt(system_prompt: str | SystemPromptFile | None) -> str:
    """Return the system-prompt TEXT whichever transport the options carry.

    The clean-room options spill the prompt to a ``--system-prompt-file`` (a
    :class:`SystemPromptFile`); this reads it back so callers (and tests) can
    assert on the actual content the CLI receives regardless of whether it is an
    inline string or a file reference. Must be called while the spilled file still
    exists (inside the ``isolated_claude_env`` scope).
    """
    if system_prompt is None:
        return ""
    if isinstance(system_prompt, str):
        return system_prompt
    return Path(system_prompt["path"]).read_text(encoding="utf-8")


__all__ = [
    "resolve_system_prompt",
    "spill_system_prompt",
]
