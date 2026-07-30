"""Production-hook plugin wiring for the clean-room SDK eval runner.

A ``production_hooks`` scenario measures the model+hook SYSTEM that ships, not
the raw model. Three seams make that real, composed by
:func:`teatree.eval.api_runner.build_sdk_options` and the runner:

*   :func:`t3_plugin` — registers the shipped teatree hook chain
    (``hooks/hooks.json`` fired from the repo-root plugin manifest) into the
    SDK child;
*   :func:`hooked_env` — redirects the hook/loop state roots into the sandbox
    home so the gates fire against fresh, owner-less state and never pollute
    the host;
*   :func:`has_hook_events` — the fail-loud signal that the plugin genuinely
    registered (its absence on a hooked run means the lane silently degraded
    back to raw-model measurement).
"""

from pathlib import Path

from claude_agent_sdk import Message
from claude_agent_sdk.types import HookEventMessage, SdkPluginConfig


def teatree_root() -> Path:
    """Return the teatree repo root (parent of ``src/teatree``)."""
    return Path(__file__).resolve().parents[3]


def t3_plugin() -> SdkPluginConfig:
    """The local-plugin config for the shipped teatree hook chain (repo root = plugin root).

    ``.claude-plugin/plugin.json`` sits at the teatree repo root and
    ``hooks/hooks.json`` fires the byte-identical shipped hook chain from the
    plugin manifest, so registering ``{"type":"local","path":<repo root>}`` makes a
    ``production_hooks`` scenario measure the model+hook SYSTEM. This is the same
    plugin lever the eval-only skill-catalog fixture plugin uses; a plugin-carried
    ``hooks.json`` fires despite ``settings='{"hooks":{}}'`` (which only empties
    USER-level hooks). Resolved against :func:`teatree_root`, not the process cwd.
    """
    return {"type": "local", "path": str(teatree_root())}


def hooked_env(env: dict[str, str], home: str) -> dict[str, str]:
    """Return *env* with the hook/loop state roots redirected into the sandbox *home*.

    :func:`~teatree.eval.isolation.isolated_claude_env` redirects
    HOME/XDG_CONFIG_HOME/CLAUDE_CONFIG_DIR, but the loop-owner registry the #807
    Stop gate consults resolves via ``XDG_DATA_HOME`` (else ``$HOME/.local/share``)
    and the hook state dir via ``T3_HOOK_STATE_DIR`` /
    ``TEATREE_CLAUDE_STATUSLINE_STATE_DIR`` — an INHERITED real value would let the
    developer's LIVE loop-owner registry make ``_session_drives_loop(eval-session)``
    False, silently SKIPPING the Stop gate (a spurious raw-model measurement) and
    polluting host hook state. Pinning all four at the sandbox home gives the gate a
    fresh, owner-less registry so it fires, and keeps eval hook state off the host.
    """
    hooked = dict(env)
    base = Path(home)
    hooked["XDG_DATA_HOME"] = str(base / ".local" / "share")
    hooked["T3_LOOP_REGISTRY_DIR"] = str(base / "loop-registry")
    hooked["T3_HOOK_STATE_DIR"] = str(base / "hook-state")
    hooked["TEATREE_CLAUDE_STATUSLINE_STATE_DIR"] = str(base / "statusline-state")
    return hooked


def has_hook_events(messages: list[Message]) -> bool:
    """Whether the captured stream carries ANY production-hook lifecycle event.

    The presence of even one ``HookEventMessage`` (started OR response) proves the
    shipped plugin's ``hooks.json`` registered and fired under the eval wiring; its
    total absence on a ``production_hooks`` run is the silent-degradation signal the
    fail-loud ``hooks_not_registered`` guard catches.
    """
    return any(isinstance(message, HookEventMessage) for message in messages)
