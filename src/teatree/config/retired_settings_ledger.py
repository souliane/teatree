"""The roll of retirements — every DB-home key that no longer names a live field.

One entry per retirement, appended forever, so it is kept apart from the resolver-facing
surface in :mod:`teatree.config.retired_settings` that derives its registries from it.

*replacement* set means the key was RENAMED and its stored value MIGRATES onto that
field; ``None`` means it was REMOVED and a stored row resolves to the default after a
loud stderr line. A rename ALSO wants a data migration rewriting the stored rows onto the
new key, so the alias is a safety net rather than the mechanism — see
``core/migrations/0027_generic_openai_compatible_backend.py``.

A roll that grows forever outgrows the module-health LOC cap, so it is split by what
answers the retired question now: a key that gated, timed, or bounded one loop or
scanner unit is appended to :mod:`teatree.config.retired_settings_loop_owned`, and
everything else — a constant, an env var, another key, a retired subsystem — is
appended here. ``RETIRED_SETTINGS`` is both halves, and the order within it carries no
meaning: every reader indexes it by key.
"""

from teatree.config.retired_settings_loop_owned import LOOP_OWNED_RETIREMENTS
from teatree.config.retirement_record import RetiredSetting

RETIRED_SETTINGS: tuple[RetiredSetting, ...] = (
    *LOOP_OWNED_RETIREMENTS,
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
        reason="branch prefixes resolve from T3_BRANCH_PREFIX / git config user.name, never a setting (#2731)",
    ),
    RetiredSetting(
        key="ask_before_post_on_behalf",
        reason="on-behalf gating resolves through the active posture's Mode.egress (#2731)",
    ),
    RetiredSetting(
        key="on_behalf_post_mode",
        reason="Mode.egress is the only control over the owner voice; a second dial never resolved against it",
    ),
    RetiredSetting(
        key="worktrees_dir",
        reason="the worktree root resolves through workspace_dir (#2731)",
    ),
    RetiredSetting(
        key="eval_credential",
        reason="the eval lane's credential follows agent_harness_provider (#3527)",
    ),
    RetiredSetting(
        key="teams_enabled",
        reason="the agent-teams pane layer is retired — nothing spawns a teammate pane (#3734)",
        subsystem="team",
    ),
    RetiredSetting(
        key="teams_max_panes",
        reason="the agent-teams pane layer is retired — nothing spawns a teammate pane (#3734)",
        subsystem="team",
    ),
    RetiredSetting(
        key="teams_idle_minutes",
        reason="the agent-teams pane layer is retired — nothing spawns a teammate pane (#3734)",
        subsystem="team",
    ),
    RetiredSetting(
        key="teams_display",
        reason="the agent-teams pane layer is retired — nothing spawns a teammate pane (#3734)",
        subsystem="team",
    ),
    RetiredSetting(
        key="availability_schedule",
        reason="the availability surface was cut in favour of presets; nothing has resolved this key since (#4203)",
    ),
    RetiredSetting(
        key="privacy",
        reason="scan strictness resolves from the `T3_PRIVACY` env var; this key never had a production reader (#4203)",
    ),
    RetiredSetting(
        key="timezone",
        reason="each schedule carries its own zone, set with `t3 loop schedule set-timezone` (#4203)",
    ),
    # Neither successor is a `UserSettings` field — both are raw `ConfigSetting` keys the
    # preset layer reads directly — so `replacement` cannot name one: it is what a stored
    # value FOLDS ONTO, and there is no field to fold onto. Migration 0086 renames the
    # stored row; these entries are for the human who types the old name and would
    # otherwise get the unknown-key refusal that reads like a typo.
    RetiredSetting(
        key="low_power_auto_engage",
        reason="renamed `token_outage_auto_engage` — the posture it arms is `token-outage` (#4202)",
    ),
    RetiredSetting(
        key="low_power_preset_name",
        reason="renamed `token_outage_preset_name` — the posture it points at is `token-outage` (#4202)",
    ),
    RetiredSetting(
        key="headless_max_turns",
        replacement="agent_max_turns",
        reason="there is one execution lane, so the ceiling qualifies nothing; the value set is identical (#4212)",
    ),
    RetiredSetting(
        key="worktree_occupancy_lease_seconds",
        reason=(
            "the claim TTL is protocol — 30 minutes is 30 renewals of the 60s run heartbeat — so it "
            "is a constant in `core/worktree/occupancy.py`. `worktree_occupancy_gate_enabled` is "
            "still the never-lockout switch, and `t3 <overlay> worktree claim-occupancy "
            "--lease-seconds` still sets a one-off TTL"
        ),
    ),
    RetiredSetting(
        key="incoming_event_retention_days",
        reason=(
            "the finished-event prune window is a constant in `teatree.core.retention.prune` (30 days) — "
            "no box ever set it, and the lane it bounds runs only from an explicit `retention prune --apply`"
        ),
    ),
    RetiredSetting(
        key="park_attempt_retention_days",
        reason=(
            "the limit-park audit window is a constant in `teatree.core.retention.prune` (7 days) — "
            "a park's diagnostic value is answered by the 24h park-spin detector and `park_repeats`, "
            "no box ever set it, and the lane runs only from an explicit `retention prune --apply`"
        ),
    ),
    RetiredSetting(
        key="deferred_question_age_ceiling_days",
        reason=(
            "the escalation backstop is permanently armed at a constant in `teatree.loop.question_drain` "
            "(3 days) — directive #45 is not a per-box opinion, and no box ever set the key"
        ),
    ),
    RetiredSetting(
        key="work_group_max_members",
        reason=(
            "the oversize-group bound is a fixed heuristic in the batch gate now — past a dozen "
            "members the shared signal is a coincidence whatever the box, so there was nothing to tune"
        ),
    ),
    RetiredSetting(
        key="work_group_generic_scopes",
        reason=(
            "the too-generic commit scopes are a fixed list in `core/review/work_group` now — "
            "`chore` / `ci` / `deps` say nothing about being one unit of work in any repo"
        ),
    ),
    RetiredSetting(
        key="review_request_dedup_window_days",
        reason=(
            "the live-Slack dedup window is the guard's own 30-day constant now; the setting was a "
            "second copy of a value the module already held as its fallback"
        ),
    ),
    RetiredSetting(
        key="review_request_dedup_max_pages",
        reason=(
            "the channel-scan page cap is the guard's own 5-page constant now; the setting was a "
            "second copy of a value the module already held as its fallback"
        ),
    ),
    RetiredSetting(
        key="review_pause_reaction_emojis",
        reason=(
            "the pause reactions are a fixed pair in `core/review/review_pause` now — react with "
            "`:double_vertical_bar:` or `:pause_button:` on the request's root message to hold it"
        ),
    ),
    RetiredSetting(
        key="mr_state_questions_max_per_tick",
        reason=(
            "the anti-spam bound on merge-request state questions is protocol, not per-box policy — "
            "`teatree.core.review.mr_state_question.MAX_OPEN_QUESTIONS` (2) holds it, and a refused "
            "merge request is still re-offered on a later tick once a slot frees"
        ),
    ),
    RetiredSetting(
        key="deny_circuit_breaker_threshold",
        reason=(
            "the K at which an identical deny trips the breaker is breaker protocol, not operator "
            "policy — `minimum=1` meant it could never disarm the breaker, and "
            "`deny_circuit_breaker_enabled` is the switch that actually relaxes it"
        ),
    ),
    RetiredSetting(
        key="venv_idle_days",
        replacement="artifact_idle_days",
        reason=(
            "a `.venv` and a `node_modules` are the same object — a rebuildable build product one "
            "command restores — and no operator intent evicts one after 2 days and the other after 7, "
            "so the window widened to every artifact name and took the venv-only name with it"
        ),
    ),
    RetiredSetting(
        key="limit_autorecovery_enabled",
        reason=(
            "park-not-fail on an exhausted Claude usage window is permanent — #3691 graduated the flag "
            "default-ON, and its OFF state only restored the terminal-FAILED behaviour that left the "
            "headless plane idle until a human poked it"
        ),
    ),
)
