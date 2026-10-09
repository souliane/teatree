"""In-session Claude account-switch detection and recovery (#1916).

When the user runs ``/login`` to switch the active Claude Code account mid session, two
caches keep answering for the OLD account: the in-process backend cache, and the cached
per-account token-health verdicts — an exhausted verdict is trusted until its blocking
window resets, so the governor would keep denying every dispatch on the old account's
exhaustion (#4736). This module detects the switch, invalidates both, and records the new
account so the switch is handled once.

The account identity is the ``oauthAccount.accountUuid`` in ``~/.claude.json``.
This module is the single reader of that value (``current_account_fingerprint``
from :mod:`teatree.core.account_fingerprint`). The recorded fingerprint of the
last-recovered account lives in a durable JSON sidecar so the check survives
compaction.

Consumed by the ``SessionStart`` hook (heartbeat every session) and the
``t3 doctor`` / ``t3 setup recover-account-switch`` CLI surfaces.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from teatree.core.account_fingerprint import current_account_fingerprint, load_recorded_fingerprint, record_fingerprint
from teatree.core.backend_factory import reset_backend_caches
from teatree.core.models.anthropic_token_usage import AnthropicTokenUsage

logger = logging.getLogger(__name__)

type CacheReset = Callable[[], None]
type TokenHealthExpiry = Callable[[], int]


@dataclass(frozen=True, slots=True)
class AccountSwitchOutcome:
    """``switched`` is ``True`` only when a recorded fingerprint differs from the active one (a genuine ``/login``)."""

    current_fingerprint: str
    previous_fingerprint: str
    switched: bool
    token_health_rows_expired: int = 0


def expire_token_health_cache() -> int:
    """Stale every cached per-account rate-limit verdict, returning the row count.

    An exhausted verdict is trusted until its blocking window resets (days), so after a
    switch the governor would keep denying every dispatch on the PREVIOUS account's
    exhaustion — while the operator holds a freshly-authenticated one (#4736).
    """
    return AnthropicTokenUsage.objects.expire_all()


@dataclass(frozen=True, slots=True)
class AccountSwitchRecovery:
    """The detect-and-invalidate cycle, with injectable I/O seams.

    ``reset_caches`` and ``expire_token_health`` default to the production backend
    factory / health cache; tests and the deterministic eval pass stubs so the cycle
    runs with no DB or ``pass`` store. The class owns the policy (when a switch counts,
    what to invalidate); the seams own only the I/O.
    """

    reset_caches: CacheReset = reset_backend_caches
    expire_token_health: TokenHealthExpiry = expire_token_health_cache

    def run(self, *, home: Path | None = None) -> AccountSwitchOutcome:
        home = home if home is not None else Path.home()
        current = current_account_fingerprint(home=home)
        previous = load_recorded_fingerprint(home=home)
        switched = bool(current) and bool(previous) and current != previous

        expired = 0
        if switched:
            logger.info(
                "account switch detected: %s -> %s; invalidating backend + token-health caches", previous, current
            )
            self.reset_caches()
            expired = self.expire_token_health()

        if current:
            record_fingerprint(current, home=home)

        return AccountSwitchOutcome(
            current_fingerprint=current,
            previous_fingerprint=previous,
            switched=switched,
            token_health_rows_expired=expired,
        )


def detect_and_recover_account_switch(*, home: Path | None = None) -> AccountSwitchOutcome:
    """Detect a ``/login`` switch, invalidate the caches, and record the new account.

    Compares the active account fingerprint against the last-recorded one. On a genuine
    switch (both non-empty and different) the backend cache is reset and the cached
    per-account token health expired. The active fingerprint is then recorded, so the
    next session no longer reports the switch. An empty active fingerprint ("cannot
    tell") never claims a switch and never records.
    """
    return AccountSwitchRecovery().run(home=home)


__all__ = [
    "AccountSwitchOutcome",
    "AccountSwitchRecovery",
    "current_account_fingerprint",
    "detect_and_recover_account_switch",
    "expire_token_health_cache",
    "load_recorded_fingerprint",
    "record_fingerprint",
]
