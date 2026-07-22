"""Owner-intake loops the fleet role policy must never mask off (#3632).

The deploy entrypoint's ``apply_fleet_loop_policy`` declares this box's per-loop
role by forcing the ``TEATREE_DISABLED_LOOPS`` set OFF on every deploy (a durable
``LoopState`` override that beats preset + base config). That reseed re-applies on
every redeploy, so a loop listed there is re-masked each time the stack comes up —
an operator's ``t3 loop override <name> on`` is wiped by the next deploy.

``directive_loop`` (interprets the owner's captured directives), ``dispatch`` (the
scanner that POSTS deferred owner questions), and ``inbox`` (ingests the owner's
inbound DMs) are OWNER-INTAKE loops: they read and interpret the owner's own
intent, they do not reach a colleague. ``autonomous_away`` means the human is
unreachable *right now* — a directive or question must QUEUE for later, not be
dropped unread. Masking the intake loops means the owner's captured intent is
never even ingested (20 owner directives sat uninterpreted for ~8 days that way).
So these loops are structurally exempt from the fleet DISABLED set: interpretation
proceeds and any ratify question waits in the queue.

The emergency handle is untouched — an operator can still ``t3 loop override
directive_loop off`` by hand. Only the redeploy-reapplied fleet role policy is
constrained here.
"""

from collections.abc import Iterable

#: Owner-intent loops that INGEST / INTERPRET the owner's captured intent. Never
#: fleet-masked off, so owner intent is always at least ingested even under an
#: ``autonomous_away`` (unattended) posture.
OWNER_INTAKE_LOOPS: frozenset[str] = frozenset({"directive_loop", "dispatch", "inbox"})

#: The fleet-role defaults the deploy entrypoint applies (mirrored verbatim in
#: ``deploy/entrypoint.sh``). The DM-only box runs its own ``inbox`` and forces the
#: COLLEAGUE-facing ``review`` loop off; ``directive_loop`` is owner-intake and is
#: no longer in the disabled default (the #3632 fix).
DEFAULT_FLEET_ENABLED: tuple[str, ...] = ("inbox",)
DEFAULT_FLEET_DISABLED: tuple[str, ...] = ("review",)


def fleet_disable_set(disabled: Iterable[str], *, enabled: Iterable[str]) -> list[str]:
    """The fleet DISABLED set with owner-intake and enabled-overlap loops removed.

    Order-preserving and deduped. A name is dropped when it is (1) also in the
    ENABLED set — the enable pass wins, forcing it off would re-mask it every
    deploy — or (2) an :data:`OWNER_INTAKE_LOOPS` member, which must stay runnable
    so the owner's intent is always ingested. The result is the set the entrypoint
    force-OFFs; the pruned intake/overlap loops keep their preset/base verdict.
    """
    enabled_set = set(enabled)
    seen: set[str] = set()
    kept: list[str] = []
    for name in disabled:
        if name in seen or name in enabled_set or name in OWNER_INTAKE_LOOPS:
            continue
        seen.add(name)
        kept.append(name)
    return kept


__all__ = [
    "DEFAULT_FLEET_DISABLED",
    "DEFAULT_FLEET_ENABLED",
    "OWNER_INTAKE_LOOPS",
    "fleet_disable_set",
]
