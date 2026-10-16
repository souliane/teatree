r"""The ONE headroom ranking — which of several accounts has the most room left.

Three selectors ask this same question of different inputs: the live routing selector
(``teatree.credential_config``, ranking cached health rows), the CI account switcher
(``teatree.ci_oauth_switch``, ranking rendered report rows) and the eval selector
(``teatree.eval.oauth_selection``, ranking freshly-probed snapshots). The arithmetic is
identical in all three, so it lives here once and they delegate; only their differing
ELIGIBILITY rules stay local.

A dependency-free foundation leaf (``teatree.llm.rate_limits`` only) with no Django
import, because the eval selector runs in a CI step BEFORE ``django.setup()`` and could
not otherwise reach a shared home.

The rank is: most binding headroom first, then the weighted blend, then the caller's own
declared :attr:`AccountHeadroom.order`, then the account name. ``order`` is what lets one
key serve all three — it is the input position for the eval selector, the operator's
configured list position for the live selector, and a uniform ``0`` for the CI switcher,
whose key then reduces exactly to the headroom pair plus the name.

TOKEN-FREE: ``account`` is a ``pass`` entry path or a positional ``token[N]`` label, never
a token value.
"""

import datetime as dt
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from teatree.llm.rate_limits import used_fraction

#: The TIE-BREAK blend, applied only between accounts whose binding window is equally
#: free. The weekly window weighs more because a long run outlasts several 5h windows
#: but never the weekly one.
WEIGHT_5H = 0.4
WEIGHT_7D = 0.6


@dataclass(frozen=True)
class AccountHeadroom:
    """One eligible account's headroom at a chosen instant, and its resulting score."""

    account: str
    order: int
    utilization_5h: float
    utilization_7d: float
    headroom_5h: float
    headroom_7d: float
    resets_before_run: bool

    @property
    def binding_headroom(self) -> float:
        """The scarcer window's free fraction — what actually throttles the run."""
        return min(self.headroom_5h, self.headroom_7d)

    @property
    def weighted_headroom(self) -> float:
        """Total headroom, blended — the tie-break between equally-constrained accounts."""
        return WEIGHT_5H * self.headroom_5h + WEIGHT_7D * self.headroom_7d


def headroom_at(utilization: float | None, reset: dt.datetime | None, at: dt.datetime) -> tuple[float, bool]:
    """The window's free fraction at *at*, and whether it resets by then.

    A window resetting at or before *at* is fully free by then, however spent it reads
    now — that projection is the whole point of taking the reset timestamps into account.
    """
    if reset is not None and reset <= at:
        return 1.0, True
    return max(0.0, 1.0 - used_fraction(utilization)), False


def sort_key(entry: AccountHeadroom) -> tuple[float, float, int, str]:
    """The ONE ordering, exported so a caller ranking its OWN row type reuses it verbatim."""
    return (-entry.binding_headroom, -entry.weighted_headroom, entry.order, entry.account)


def rank(entries: Iterable[AccountHeadroom]) -> tuple[AccountHeadroom, ...]:
    """*entries* richest-first, deterministically — equal health always ranks the same way."""
    return tuple(sorted(entries, key=sort_key))


@dataclass(frozen=True)
class WindowReserve:
    min_5h_free: float = 0.40
    min_7d_free: float = 0.30

    def __post_init__(self) -> None:
        for value in (self.min_5h_free, self.min_7d_free):
            if not math.isfinite(value) or not 0 <= value <= 1:
                msg = "eval reserve fractions must be finite values in [0, 1]"
                raise ValueError(msg)

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> "WindowReserve":
        def read(name: str, default: float) -> float:
            raw = env.get(name, "").strip()
            return float(raw) if raw else default

        return cls(
            min_5h_free=read("EVAL_RESERVE_5H_FREE", cls.min_5h_free),
            min_7d_free=read("EVAL_RESERVE_7D_FREE", cls.min_7d_free),
        )

    def allows(
        self,
        utilization_5h: float | None,
        reset_5h: dt.datetime | None,
        utilization_7d: float | None,
        reset_7d: dt.datetime | None,
        at: dt.datetime,
    ) -> bool:
        if utilization_5h is None or utilization_7d is None:
            return False
        free_5h = headroom_at(utilization_5h, reset_5h, at)[0]
        free_7d = headroom_at(utilization_7d, reset_7d, at)[0]
        return free_5h >= self.min_5h_free and free_7d >= self.min_7d_free
