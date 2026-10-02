"""Keep the ``claude`` CLI child spawnable — the system prompt travels by FILE, not argv.

The SDK's subprocess transport renders ``ClaudeAgentOptions.system_prompt``'s
``append`` as a single ``--append-system-prompt <text>`` argv element, so the spawn
payload grows with every skill teatree loads. Past the kernel's per-argument cap the
child dies at ``execve`` with ``[Errno 7]`` before reading a byte of the task —
deterministically, so every retry fails identically and the phase is dead.

:func:`prepared_spawn` writes that append to a temp file and points the CLI at it with
``--append-system-prompt-file``, which appends to the ``claude_code`` preset exactly as
the inline flag did — the preset is preserved, the prompt simply stops riding argv. The
file is bracketed by the context manager, so it lives exactly as long as the child.

What remains on argv (the tool denylists, the MCP config, the resolved paths) plus the
child environment is measured by :func:`preflight_payload`, and a spawn that dies anyway
is named by :func:`spawn_error` rather than surfaced as a bare errno. That measurement is
deliberately built from teatree's OWN options rather than the SDK transport's private
command builder: it is a floor every consumer here is sound under, and it costs no
dependency on an internal that can change under us.
"""

import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import SystemPromptPreset

from teatree.agents.spawn_payload import (
    AgentSpawnError,
    SpawnPayload,
    e2big_message,
    is_e2big,
    measure_spawn_payload,
    spawn_refusal_reason,
)

#: The CLI flag that appends a file's contents to the default system prompt. Passed
#: through ``extra_args`` because the SDK's own ``SystemPromptPreset`` only renders the
#: inline ``--append-system-prompt``, and its ``SystemPromptFile`` REPLACES the preset.
APPEND_PROMPT_FILE_FLAG = "append-system-prompt-file"
_PROMPT_FILE_PREFIX = "t3-system-prompt-"
_PROMPT_FILE_SUFFIX = ".md"


def _preset_append(options: ClaudeAgentOptions) -> str:
    prompt = options.system_prompt
    if isinstance(prompt, dict) and prompt.get("type") == "preset":
        return str(prompt.get("append") or "")
    return ""


def _without_append(prompt: SystemPromptPreset) -> SystemPromptPreset:
    """The same preset minus ``append``, so the transport emits no system-prompt argv."""
    return SystemPromptPreset(**{k: v for k, v in prompt.items() if k != "append"})  # ty: ignore[missing-typed-dict-key]


@contextmanager
def prepared_spawn(options: ClaudeAgentOptions) -> Iterator[ClaudeAgentOptions]:
    """Yield *options* with the system-prompt append moved off argv into a temp file.

    A no-op yield of the original *options* when there is no preset append to move
    (an inline-string prompt, a file prompt, a resumed spawn with none) — those carry
    no teatree-grown argv element, so rewriting them would change a transport that
    is not implicated.
    """
    append = _preset_append(options)
    if not append:
        yield options
        return
    with tempfile.TemporaryDirectory(prefix=_PROMPT_FILE_PREFIX) as directory:
        path = Path(directory) / f"system-prompt{_PROMPT_FILE_SUFFIX}"
        path.write_text(append, encoding="utf-8")
        preset = _without_append(options.system_prompt)  # ty: ignore[invalid-argument-type]
        yield replace(
            options,
            system_prompt=preset,
            extra_args={**options.extra_args, APPEND_PROMPT_FILE_FLAG: str(path)},
        )


def option_argument_strings(options: ClaudeAgentOptions) -> list[str]:
    """Every argv string these *options* contribute — the growable half of the vector.

    A FLOOR, not the whole command: the SDK adds its own short, constant flags. Every
    string that grows with teatree's configuration is here, so a measurement built on it
    is sound in the direction that matters — it can under-report a payload that fits,
    never over-report one that does not.
    """
    prompt = options.system_prompt
    strings = [prompt] if isinstance(prompt, str) else [str(value) for value in (prompt or {}).values()]
    strings += [",".join(options.disallowed_tools), ",".join(options.allowed_tools)]
    strings += [str(directory) for directory in options.add_dirs]
    strings += [str(options.cwd or ""), options.model or "", options.fallback_model or "", options.resume or ""]
    strings += [f"{flag}={value or ''}" for flag, value in options.extra_args.items()]
    strings.append(json.dumps(options.mcp_servers, default=str))
    return [text for text in strings if text]


def child_env(options: ClaudeAgentOptions) -> Mapping[str, str]:
    """The environment the transport hands the child — the ambient env under ``options.env``.

    Counted in full because envp shares the one ``ARG_MAX`` budget with argv: a payload
    judged on argv alone reads as healthy right up to the spawn that fails.
    """
    return {**os.environ, **options.env}


def preflight_payload(options: ClaudeAgentOptions, env: Mapping[str, str] | None = None) -> SpawnPayload:
    """Measure what this spawn would charge ``execve``: the option arguments plus the child *env*."""
    return measure_spawn_payload(option_argument_strings(options), child_env(options) if env is None else env)


def assert_spawnable(options: ClaudeAgentOptions, env: Mapping[str, str] | None = None) -> SpawnPayload:
    """Return the measured payload, raising :class:`AgentSpawnError` when it cannot be sent.

    Refuses only on a breach the FLOOR already proves, so an under-measured payload never
    blocks a dispatch that would have worked — the kernel stays the authority, and
    :func:`spawn_error` names whatever it rejects beyond that.
    """
    payload = preflight_payload(options, env)
    if spawn_refusal_reason(payload):
        raise AgentSpawnError(e2big_message(payload))
    return payload


def spawn_error(exc: BaseException, payload: SpawnPayload) -> AgentSpawnError | None:
    """Name an E2BIG spawn death from *exc*, or ``None`` when it is any other failure.

    Reads the whole exception chain: the SDK wraps the ``OSError`` in a
    ``CLIConnectionError`` whose text is the only place ``[Errno 7]`` survives.
    """
    chain: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        chain.append(str(current))
        current = current.__cause__ or current.__context__
    if not is_e2big("\n".join(chain)):
        return None
    return AgentSpawnError(e2big_message(payload))
