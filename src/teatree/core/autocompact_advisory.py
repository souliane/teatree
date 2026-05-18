"""Advisory for a Claude Code harness silent kill-switch (issue #980).

For 1M-capable models (currently ``claude-opus-4-7``) without an
explicit auto-compact window setting, the harness in
``@anthropic-ai/claude-code`` v2.x silently disables auto-compaction
regardless of ``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE``.

The harness logic, decoded from the bundled binary at
``$(npm root -g)/@anthropic-ai/claude-code/bin/claude.exe``::

    function zKH(H) {
        if (!Nw_()) return false;
        if (hP(H, fD()) !== 1e6) return false;     // model max-window is 1M
        let _ = Z7(H);
        if (D96(_) !== void 0) return false;        // statsig flag is set
        return true;                                // → "kill-switch trips"
    }

    function oiH(H, _) {
        let {source: q} = Kr(H, _);
        return q === "env" || q === "settings";     // user explicit window
    }

    async function o13(H, _, q, K, O = 0) {
        if (!r0()) return false;                    // autoCompactEnabled
        if (zKH(_) && !oiH(_, q)) return false;     // ← THE BUG
        ...
    }

The trip condition (``zKH(model) && !oiH(model, autoCompactWindow)``)
is reached when:

1. The model is 1M-capable (``claude-opus-4-7``).
2. ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` env var is unset.
3. The ``autoCompactWindow`` setting in ``~/.claude/settings.json`` is
    unset.
4. The user is not opted into the ``tengu_amber_redwood2`` statsig
    feature flag.

When those four hold, ``o13`` returns ``false`` before consulting
``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE``: auto-compaction never fires no
matter how full the context gets. Source spec
``docs/claude-code-internals.md`` § 4 (Layer 2) calls the trigger
"~90% of context window (configurable threshold)"; the configurable
threshold is silently skipped on this path.

The fix the harness wants is for the user to set
``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` to the model's max window
(``1000000`` for ``claude-opus-4-7``). That changes
``Kr.source`` to ``"env"``, making ``oiH`` return true and bypassing
``zKH``'s kill-switch. The ``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`` then
applies as a percentage of the effective window.

Setting ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` to a value lower than the
model's max window is a foot-gun (it shrinks the trigger to a fraction
of that smaller value) — this is what motivated the advisory: the user
hit this exact wrong-direction mistake on 2026-05-18.
"""

import os
from dataclasses import dataclass

# Models for which the harness's silent kill-switch trips when no
# explicit window is configured. Kept narrow on purpose — the kill-
# switch is gated on ``hP(model, betas) === 1e6`` in the harness, and
# the only model that flips this without the ``[1m]`` beta header is
# ``claude-opus-4-7``. The ``[1m]`` suffix is stripped by the harness's
# ``CD`` normaliser before comparison, so both names are equivalent
# kill-switch carriers.
_KILL_SWITCH_MODELS: frozenset[str] = frozenset(
    {
        "claude-opus-4-7",
        "claude-opus-4-7[1m]",
    }
)

# The harness ceiling for ``claude-opus-4-7``. ``hP`` returns ``1e6``
# unconditionally for this model name (see ``JqH(H)`` and ``nZ(H)``).
_OPUS_4_7_MAX_WINDOW_TOKENS = 1_000_000

# Upper bound the harness ``gE8`` accepts for ``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE``
# (``parseFloat`` result in ``(0, 100]``).
_PCT_OVERRIDE_MAX = 100


@dataclass(frozen=True)
class AutocompactConfig:
    """Snapshot of the auto-compact-relevant harness env knobs.

    Built deliberately from environment alone — the harness reads its
    own settings.json itself; we only need to see the knobs the user
    has set explicitly to decide whether the kill-switch would trip.
    """

    pct_override: str | None
    auto_compact_window: str | None
    disable_compact: str | None
    disable_auto_compact: str | None
    model: str | None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AutocompactConfig":
        src = env if env is not None else os.environ
        return cls(
            pct_override=src.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"),
            auto_compact_window=src.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"),
            disable_compact=src.get("DISABLE_COMPACT"),
            disable_auto_compact=src.get("DISABLE_AUTO_COMPACT"),
            model=src.get("CLAUDE_CODE_MODEL") or src.get("ANTHROPIC_MODEL"),
        )


def _is_truthy(value: str | None) -> bool:
    """Match the harness's ``xH`` truthy-env-var helper (``"1"``/``"true"``)."""
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _is_kill_switch_model(model: str | None) -> bool:
    """Return True for models whose ``hP(model, betas)`` returns 1M.

    The harness's ``CD`` normaliser strips ``[1m]`` and lowercases the
    name; mirror that here so the detection holds for any cased
    variant the agent might be running under.
    """
    if not model:
        return False
    return model.strip().lower() in _KILL_SWITCH_MODELS


def has_pct_override(config: AutocompactConfig) -> bool:
    """Return True iff ``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`` is set to a usable number.

    Matches the harness's ``gE8`` parsing: a numeric ``parseFloat``
    result in ``(0, 100]`` enables the override. Empty / non-numeric /
    out-of-range values fall back to the default and ARE NOT a
    user-expressed preference, so we don't surface the advisory for
    them.
    """
    raw = config.pct_override
    if raw is None or not raw.strip():
        return False
    try:
        value = float(raw)
    except ValueError:
        return False
    return 0 < value <= _PCT_OVERRIDE_MAX


def kill_switch_trips(config: AutocompactConfig) -> bool:
    """Return True iff the harness will silently drop auto-compact.

    Decides on the env-var combo alone. Reproduces the harness logic
    in :mod:`teatree.core.autocompact_advisory` docstring.
    """
    if _is_truthy(config.disable_compact) or _is_truthy(config.disable_auto_compact):
        # User has globally disabled compaction; not the kill-switch
        # bug, just an explicit opt-out.
        return False
    if not has_pct_override(config):
        # No user-configured threshold to silently drop.
        return False
    if config.auto_compact_window and config.auto_compact_window.strip():
        # An explicit window already opts ``Kr.source`` to "env",
        # bypassing the kill-switch.
        return False
    return _is_kill_switch_model(config.model)


def recommended_env_var() -> tuple[str, str]:
    """Return the env-var name and value that bypass the kill-switch.

    Setting ``CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000`` flips
    ``Kr.source`` to ``"env"`` so ``oiH`` returns true and the
    ``zKH`` kill-switch is bypassed. ``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE``
    then applies as a percentage of the effective window.
    """
    return "CLAUDE_CODE_AUTO_COMPACT_WINDOW", str(_OPUS_4_7_MAX_WINDOW_TOKENS)


def advisory_text(config: AutocompactConfig) -> str | None:
    """Return the user-facing advisory, or None when no advisory is warranted.

    Designed for callers that emit ``additionalContext`` (SessionStart
    hook) and for ``t3 doctor`` text output alike — the text is the
    same content; the caller wraps it for its surface.
    """
    if not kill_switch_trips(config):
        return None
    var_name, var_value = recommended_env_var()
    pct = (config.pct_override or "").strip()
    model = (config.model or "claude-opus-4-7").strip()
    return (
        "AUTO-COMPACT SILENT KILL-SWITCH (souliane/teatree#980): "
        f"`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE={pct}` is set but the Claude "
        f"Code harness silently disables auto-compaction for `{model}` "
        "until `CLAUDE_CODE_AUTO_COMPACT_WINDOW` is ALSO set. "
        f'Add `"{var_name}": "{var_value}"` to the `env` block in '
        "`~/.claude/settings.json` to make the percentage override "
        f"effective (auto-compact will then fire at ~{pct}% of "
        f"{var_value} tokens). The harness function chain is "
        "`zKH(model) && !oiH(model, autoCompactWindow) ⇒ return false` "
        "in `o13`, before the threshold logic runs."
    )
