"""Self-repair of a deterministically-correctable config breach (#3665).

An invalid ``agent_harness`` / ``agent_harness_provider`` pair produced an
excellent diagnostic that was then posted to the owner's Slack as a repair-halt
question. The message was right; the paging was not — a config pair with exactly
one valid resolution has no decision in it, so the system corrects it and logs
rather than interrupting a person.

**The criterion, stated exactly.** A repair-halt failure is *self-correctable*
iff its error text is recognised by a resolver here AND that resolver yields
**exactly one** setting→value assignment that makes the configuration valid.
Zero candidates (nothing to correct) and two or more (a genuine choice) both
page, unchanged.

For the harness/provider pair the candidate set is the harnesses under which the
operator's EXPLICIT provider pin is valid. Correcting the *provider* instead is
deliberately NOT attempted: every harness admits two providers, so picking one
would be inventing a credential decision the operator never made. Correcting the
harness honours the most-specific pin they did make. A future harness that shares
a provider makes the set ambiguous, and the condition pages again — by
construction, not by a second rule.

Self-repair is loud, never silent: it logs at WARNING and stamps
:data:`SELF_REPAIR_STAMP` onto the repaired task so the dashboard's configuration
band can surface it. It just does not interrupt a person.

This module owns the PURE criterion and the correction it yields. The live
resolution against the harness registry lives in
:mod:`teatree.loop.config_self_repair` — the agents-layer registry and the
core-layer config store are domain siblings that cannot import each other, so
only an orchestration module may compose them.
"""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

from teatree.core.models import ConfigSetting

logger = logging.getLogger(__name__)

#: Stamped onto a repaired task's ``execution_reason``. Loud and durable: the
#: dashboard's configuration band reads it, and its presence means this task has
#: already spent its one self-repair (a recurrence escalates normally).
SELF_REPAIR_STAMP = "[self-repaired]"

_INVALID_PAIR_RE = re.compile(
    r"agent_harness_provider=['\"]?(?P<provider>[\w.-]+)['\"]? is not valid under agent_harness=",
)

_HARNESS_SETTING = "agent_harness"


@dataclass(frozen=True, slots=True)
class ConfigRepair:
    """One deterministic setting→value correction, with the reason it is unambiguous."""

    setting: str
    value: str
    detail: str

    def apply(self) -> None:
        """Write the correction into the global ``ConfigSetting`` store."""
        ConfigSetting.objects.set_value(self.setting, self.value)
        logger.warning("Self-repaired config %s=%s — %s", self.setting, self.value, self.detail)

    def stamp(self) -> str:
        """The durable, dashboard-readable marker for a task this repair unblocked."""
        return f"{SELF_REPAIR_STAMP} {self.setting}={self.value}"


def plan_config_repair(error: str, *, valid_providers_by_harness: Mapping[str, frozenset[str]]) -> ConfigRepair | None:
    """The single valid resolution for *error*, or ``None`` when a human must decide.

    Pure: the harness→valid-providers table is injected, so the criterion is
    testable against a hypothetical third harness without touching the registry.
    """
    match = _INVALID_PAIR_RE.search(error)
    if match is None:
        return None
    provider = match.group("provider")
    candidates = sorted(name for name, valid in valid_providers_by_harness.items() if provider in valid)
    if len(candidates) != 1:
        return None
    harness = candidates[0]
    return ConfigRepair(
        setting=_HARNESS_SETTING,
        value=harness,
        detail=(
            f"agent_harness_provider={provider!r} is valid under exactly one harness "
            f"({harness!r}), so the pinned provider decides the transport"
        ),
    )


__all__ = ["SELF_REPAIR_STAMP", "ConfigRepair", "plan_config_repair"]
