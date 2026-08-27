"""Production-hook plugin wiring for the clean-room SDK eval runner.

A ``production_hooks`` scenario measures the model+hook SYSTEM that ships, not
the raw model. Three seams make that real, composed by
:func:`teatree.eval.api_runner.build_sdk_options` and the runner:

*   :func:`t3_plugin` — registers the shipped teatree hook chain
    (``hooks/hooks.json`` fired from the repo-root plugin manifest) into the
    SDK child;
*   :func:`hooked_env` — resolves ``CLAUDE_PLUGIN_ROOT`` and redirects the
    hook/loop state roots into the sandbox home so the gates fire against
    fresh, owner-less state and never pollute the host;
*   :func:`has_hook_events` — the fail-loud signal that the plugin genuinely
    registered (its absence on a hooked run means the lane silently degraded
    back to raw-model measurement).
"""

import json
import shlex
from pathlib import Path

from claude_agent_sdk import Message
from claude_agent_sdk.types import HookEventMessage, SdkPluginConfig

PLUGIN_ROOT_VAR = "CLAUDE_PLUGIN_ROOT"
_PLUGIN_ROOT_PLACEHOLDER = f"${{{PLUGIN_ROOT_VAR}}}"


class PluginRootUnresolvedError(RuntimeError):
    """The hooked lane cannot expand ``${CLAUDE_PLUGIN_ROOT}`` into runnable hook commands."""


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


def hook_script_paths(root: Path) -> set[Path]:
    """Every ``hooks.json`` command's script path, expanded against *root*."""
    manifest = json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    return {
        Path(shlex.split(hook["command"].replace(_PLUGIN_ROOT_PLACEHOLDER, str(root)))[0])
        for matchers in manifest.get("hooks", {}).values()
        for matcher in matchers
        for hook in matcher.get("hooks", ())
        if hook.get("type") == "command"
    }


def preflighted_plugin_root() -> Path:
    """The absolute root every ``hooks.json`` command interpolates, proven to resolve.

    ``${CLAUDE_PLUGIN_ROOT}`` is expanded by the SHELL each hook command runs in, so a
    child that does not carry the variable executes ``/hooks/scripts/run-hook.sh``,
    every gate dies at ``Hook cancelled``, and the scenarios time out at
    ``initialize`` — six behavioural reds for one wiring fault. Raising here makes the
    lane fail loudly with the unresolved path named.
    """
    root = teatree_root()
    unresolved = sorted(str(path) for path in hook_script_paths(root) if not path.is_file())
    if unresolved:
        msg = (
            f"production_hooks lane is unwired: {PLUGIN_ROOT_VAR}={root} leaves these hook "
            f"commands unrunnable: {unresolved}. Every shipped gate would die at "
            "'Hook cancelled' and the lane would report behavioural reds for a wiring fault."
        )
        raise PluginRootUnresolvedError(msg)
    return root


def hooked_env(env: dict[str, str], home: str) -> dict[str, str]:
    """Return *env* with ``CLAUDE_PLUGIN_ROOT`` resolved and the state roots inside *home*.

    :func:`~teatree.eval.isolation.isolated_claude_env` redirects
    HOME/XDG_CONFIG_HOME/CLAUDE_CONFIG_DIR, but the loop-owner registry the #807
    Stop gate consults resolves via ``XDG_DATA_HOME`` (else ``$HOME/.local/share``)
    and the hook state dir via ``T3_HOOK_STATE_DIR`` /
    ``TEATREE_CLAUDE_STATUSLINE_STATE_DIR`` — an INHERITED real value would let the
    developer's LIVE loop-owner registry make ``_session_drives_loop(eval-session)``
    False, silently SKIPPING the Stop gate (a spurious raw-model measurement) and
    polluting host hook state. Pinning those four at the sandbox home gives the gate a
    fresh, owner-less registry so it fires, and keeps eval hook state off the host.
    ``CLAUDE_PLUGIN_ROOT`` is the fifth and is preflighted, not sandboxed — it points
    at the shipped plugin the hook commands live in.
    """
    hooked = dict(env)
    base = Path(home)
    hooked[PLUGIN_ROOT_VAR] = str(preflighted_plugin_root())
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
