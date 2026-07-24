"""Every DB-home settings key that no longer names a live ``UserSettings`` field.

souliane/teatree#3527: a retired key is invisible to the resolver — ``_coerce_db_rows``
drops any row whose key is not in the DB-home parser registry — so removing a
setting silently reverted an operator's explicitly-configured value to the
dataclass default. The removal of ``eval_credential`` reverted the configured
credential operator with nothing said.

This registry is the single place a retirement is recorded, and it admits exactly
two outcomes, both of them visible:

*   ``replacement`` set — the key was RENAMED. Its stored value MIGRATES: the row
    resolves onto the replacement field (the canonical key still wins when both
    rows exist), so the operator's opinion survives untouched.
*   ``replacement`` ``None`` — the key was REMOVED. Resolving a stored row emits a
    loud stderr line naming the key, why it went, and the remedy, then falls
    through to the default. Loud rather than fatal is deliberate: a stale row must
    never lock an operator out of their own factory (the never-lockout doctrine),
    but it must never be silent either.

A rename ALSO wants a data migration rewriting the stored rows onto the new key,
so the alias is a safety net rather than the mechanism — see
``core/migrations/0027_generic_openai_compatible_backend.py``.
"""

import sys
from dataclasses import dataclass

_CLEAR_REMEDY = "t3 <overlay> config_setting clear {key}"


@dataclass(frozen=True, slots=True)
class RetiredSetting:
    """One DB-home key that is no longer a live field, and what became of it.

    *replacement* names the current field a stored value migrates onto, or is
    ``None`` when the setting was removed outright. *reason* is rendered into the
    loud removal warning, so it is written for the operator reading it — what the
    setting used to do and what now does that job.
    """

    key: str
    replacement: str | None
    reason: str


RETIRED_SETTINGS: tuple[RetiredSetting, ...] = (
    RetiredSetting(
        key="todo_sweep_disabled",
        replacement="task_sweep_disabled",
        reason="the loop unit reconciles teatree Task rows, not the harness TODO list (#129)",
    ),
    RetiredSetting(
        key="todo_sweep_recheck_interval_hours",
        replacement="task_sweep_recheck_interval_hours",
        reason="the loop unit reconciles teatree Task rows, not the harness TODO list (#129)",
    ),
    RetiredSetting(
        key="speed",
        replacement="wip",
        reason="the throughput dial is the bounded-WIP setting; the value set is identical (#2951)",
    ),
    RetiredSetting(
        key="orca_router_pass_path",
        replacement="openai_compatible_credential_entry",
        reason="the provider-specific backend collapsed into the generic OpenAI-compatible one (#3666)",
    ),
    RetiredSetting(
        key="orca_router_name",
        replacement="openai_compatible_model",
        reason="the provider-specific backend collapsed into the generic OpenAI-compatible one (#3666)",
    ),
    RetiredSetting(
        key="orca_router_lane",
        replacement="openai_compatible_lane",
        reason="the provider-specific backend collapsed into the generic OpenAI-compatible one (#3666)",
    ),
    RetiredSetting(
        key="branch_prefix",
        replacement=None,
        reason="branch prefixes resolve from T3_BRANCH_PREFIX / git config user.name, never a setting (#2731)",
    ),
    RetiredSetting(
        key="ask_before_post_on_behalf",
        replacement=None,
        reason="on-behalf gating resolves through on_behalf_post_mode (#2731)",
    ),
    RetiredSetting(
        key="worktrees_dir",
        replacement=None,
        reason="the worktree root resolves through workspace_dir (#2731)",
    ),
    RetiredSetting(
        key="eval_credential",
        replacement=None,
        reason="the eval lane's credential follows agent_harness_provider (#3527)",
    ),
)

#: Retired key -> the live field its stored value migrates onto.
RENAMED_SETTING_KEYS: dict[str, str] = {
    entry.key: entry.replacement for entry in RETIRED_SETTINGS if entry.replacement is not None
}

#: Retired keys with no replacement — a stored row is reported loudly.
REMOVED_SETTING_KEYS: frozenset[str] = frozenset(entry.key for entry in RETIRED_SETTINGS if entry.replacement is None)

_BY_KEY: dict[str, RetiredSetting] = {entry.key: entry for entry in RETIRED_SETTINGS}


def removed_setting(key: str) -> RetiredSetting | None:
    """The removal record for *key*, or ``None`` when it is live or merely renamed."""
    entry = _BY_KEY.get(key)
    return entry if entry is not None and entry.replacement is None else None


def warn_removed_setting(entry: RetiredSetting) -> None:
    """Report a stored row under a removed key on stderr — the anti-silent-revert line.

    Named, reasoned, and actionable: without all three the operator learns only
    that something changed. Emitted per resolution rather than once per process so
    it cannot be lost to a warm import in a long-lived loop worker.
    """
    sys.stderr.write(
        f"WARNING: the config setting {entry.key!r} was removed — {entry.reason}. "
        f"Its stored value is NOT in effect and this setting has reverted to its default. "
        f"Clear the stale row with `{_CLEAR_REMEDY.format(key=entry.key)}`.\n"
    )
