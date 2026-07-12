"""The neutral, provider-agnostic option contract past the harness boundary (#3157 AH-2).

:meth:`Harness.open` still accepts the vendor ``claude_agent_sdk.ClaudeAgentOptions`` at the
seam boundary: the ``claude_sdk`` backend hands it straight to ``ClaudeSDKClient``, and
re-homing the SDK-specific surface (``mcp_servers``, hooks, tool permissions) onto a
fully-neutral ``open`` signature is the strangler-fig migration the redesign doc's
port-surface table defers to a later PR. That deferral is why the boundary type is still the
vendor one — documented on the ``Harness`` protocol.

But a PROVIDER-AGNOSTIC backend (``pydantic_ai`` today; the factory overlay's Vertex-EU binding
next) must not thread the vendor type through its OWN logic. :class:`HarnessOptions` is that
neutral value: :class:`~teatree.agents.harness.PydanticAiHarness` adapts the vendor options
into it ONCE at the top of ``open`` (:meth:`HarnessOptions.from_sdk_options`) and reads only
neutral fields afterward — model resolution, reasoning effort, the tool jail root/env — so the
vendor type never leaks past the boundary into provider-agnostic code. The factory overlay builds
its provider-agnostic dispatch against this type, not against ``ClaudeAgentOptions``.
"""

from dataclasses import dataclass, field

from claude_agent_sdk import ClaudeAgentOptions


def extract_system_prompt(options: ClaudeAgentOptions) -> str:
    """Pull the portable custom system context out of the vendor *options* (the SDK adapter).

    ``ClaudeAgentOptions.system_prompt`` is normally a ``SystemPromptPreset``
    (``{"type": "preset", "preset": "claude_code", "append": <context>}``) — the
    ``claude_code`` preset itself is meaningless outside the bundled CLI, so only the appended
    custom context is portable; a plain ``str`` (as tests build) is used as-is; anything else
    (a ``SystemPromptFile`` reference, or ``None``) has no portable content here. This is the
    one field :class:`HarnessOptions` TRANSFORMS rather than copies verbatim.
    """
    prompt = options.system_prompt
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, dict) and prompt.get("type") == "preset":
        return str(prompt.get("append", ""))
    return ""


@dataclass(frozen=True, slots=True)
class HarnessOptions:
    """The provider-agnostic subset of agent options a non-Claude backend consumes.

    *   ``model`` — the resolved model id (``None`` → the backend's own default).
    *   ``effort`` — the reasoning-effort rung, unvalidated here (the backend maps it to its
        own vocabulary; e.g. :func:`~teatree.agents.harness.resolve_effort`).
    *   ``system_prompt`` — the PLAIN custom system context (the SDK ``claude_code`` preset is
        already stripped in the adapter, so a backend uses this string as-is).
    *   ``cwd`` — the resolved task worktree (the Lane-B File System jail root).
    *   ``env`` — the pinned child-env overrides (merged over the ambient env by the tool layer).
    """

    model: str | None = None
    effort: str | None = None
    system_prompt: str = ""
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_sdk_options(cls, options: ClaudeAgentOptions) -> "HarnessOptions":
        """Adapt the vendor ``ClaudeAgentOptions`` into the neutral options at the ``open`` boundary."""
        return cls(
            model=options.model,
            effort=options.effort,
            system_prompt=extract_system_prompt(options),
            # ``ClaudeAgentOptions.cwd`` is ``str | Path | None``; the neutral type carries a
            # plain path string (a Path is more vendor/OS-coupled than its string form).
            cwd=str(options.cwd) if options.cwd else None,
            env=dict(options.env or {}),
        )
