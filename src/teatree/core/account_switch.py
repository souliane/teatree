"""In-session Claude account-switch detection and connector recovery (#1916).

When the user runs ``/login`` to switch the active Claude Code account mid
session, the in-process MCP/backend token cache keeps routing to the *old*
account: outbound Slack/Notion calls return ``ok`` but land in a workspace the
new account no longer reads, so the user sees nothing (souliane/teatree#1176,
#1239). The watchdog (``teatree.loop.watchdog``) already respawns a *new
process* on the switch; this module is the in-session sibling that the running
session itself runs — detect the switch, invalidate the backend cache, re-probe
connector reachability, and surface the result.

The account identity is the ``oauthAccount.accountUuid`` in ``~/.claude.json``
— the same fully-qualified fingerprint the watchdog pins. This module is the
single reader of that value (``current_account_fingerprint``);
``teatree.loop.watchdog`` re-exports it rather than parsing the file a second
time. The recorded fingerprint of the last-recovered account lives in a durable
JSON sidecar so the check survives compaction and a token-broken bridge.

Consumed by the ``SessionStart`` hook (heartbeat every session) and the
``t3 doctor`` / ``t3 setup recover-account-switch`` CLI surfaces. The
reachability probe re-reads each connector's live ``auth.test`` *after* the
cache reset (verify-by-re-read) so a stale cached token cannot mask a broken
bridge.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from teatree.core.account_fingerprint import current_account_fingerprint, load_recorded_fingerprint, record_fingerprint
from teatree.core.backend_factory import iter_overlay_backends, reset_backend_caches
from teatree.core.backend_protocols import MessagingBackend

logger = logging.getLogger(__name__)

type CacheReset = Callable[[], None]
type BackendsProvider = Callable[[], list[MessagingBackend]]


@dataclass(frozen=True, slots=True)
class ConnectorProbeResult:
    """One connector's post-switch reachability, from a live ``auth.test``."""

    name: str
    reachable: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AccountSwitchOutcome:
    """The result of one detect-and-recover cycle.

    ``switched`` is ``True`` only when a previously-recorded fingerprint
    differs from the now-active one (a genuine ``/login``). ``all_reachable``
    reflects the post-reset probe: ``True`` when no connectors were probed or
    every probed connector answered ``ok``.
    """

    current_fingerprint: str
    previous_fingerprint: str
    switched: bool
    probes: tuple[ConnectorProbeResult, ...] = field(default_factory=tuple)

    @property
    def all_reachable(self) -> bool:
        return all(probe.reachable for probe in self.probes)


def overlay_messaging_backends() -> list[MessagingBackend]:
    """Every registered overlay's messaging backend, built fresh from config."""
    return [b.messaging for b in iter_overlay_backends() if b.messaging is not None]


def probe_connectors(backends: list[MessagingBackend]) -> list[ConnectorProbeResult]:
    """Live-probe each backend's ``auth.test`` and classify reachability.

    Called *after* the cache reset so each probe reads the connector's current
    truth, not a stale cached token. A backend whose ``auth.test`` raises or
    returns falsy ``ok`` is unreachable with the error in ``detail``.
    """
    results: list[ConnectorProbeResult] = []
    for backend in backends:
        name = getattr(backend, "name", backend.__class__.__name__)
        try:
            response = backend.auth_test()
        except Exception as exc:  # noqa: BLE001 — any transport failure is "unreachable", never a crash
            results.append(ConnectorProbeResult(name=name, reachable=False, detail=f"{type(exc).__name__}: {exc}"))
            continue
        if response.get("ok"):
            results.append(ConnectorProbeResult(name=name, reachable=True))
        else:
            results.append(
                ConnectorProbeResult(name=name, reachable=False, detail=str(response.get("error", "unknown error"))),
            )
    return results


@dataclass(frozen=True, slots=True)
class AccountSwitchRecovery:
    """The detect-invalidate-reprobe cycle, with injectable I/O seams.

    ``reset_caches`` and ``backends`` default to the production overlay factory;
    tests and the deterministic eval pass stubs so the cycle runs with no
    network or ``pass`` store. The class owns the policy (when a switch counts,
    what to invalidate, what to probe); the seams own only the I/O.
    """

    reset_caches: CacheReset = reset_backend_caches
    backends: BackendsProvider = overlay_messaging_backends

    def run(self, *, home: Path | None = None) -> AccountSwitchOutcome:
        home = home if home is not None else Path.home()
        current = current_account_fingerprint(home=home)
        previous = load_recorded_fingerprint(home=home)
        switched = bool(current) and bool(previous) and current != previous

        probes: tuple[ConnectorProbeResult, ...] = ()
        if switched:
            logger.info("account switch detected: %s -> %s; invalidating backend cache", previous, current)
            self.reset_caches()
            probes = tuple(probe_connectors(self.backends()))

        outcome = AccountSwitchOutcome(
            current_fingerprint=current,
            previous_fingerprint=previous,
            switched=switched,
            probes=probes,
        )

        if current and (not switched or outcome.all_reachable):
            record_fingerprint(current, home=home)

        return outcome


def detect_and_recover_account_switch(*, home: Path | None = None) -> AccountSwitchOutcome:
    """Detect a ``/login`` switch, invalidate the cache, and re-probe connectors.

    Compares the active account fingerprint against the last-recorded one. On a
    genuine switch (both non-empty and different): reset the backend cache and
    re-probe each messaging connector's live reachability. The new fingerprint
    is recorded only when recovery genuinely succeeded — a no-switch run (first
    run or unchanged account) or a switch where every connector probed
    reachable. A switch that left a connector unreachable does NOT record, so
    the next session re-detects the switch and the heartbeat keeps surfacing
    until the bridge is actually fixed. An empty active fingerprint ("cannot
    tell") never claims a switch and never records. Thin convenience wrapper
    around :class:`AccountSwitchRecovery` with production seams.
    """
    return AccountSwitchRecovery().run(home=home)


__all__ = [
    "AccountSwitchOutcome",
    "AccountSwitchRecovery",
    "ConnectorProbeResult",
    "current_account_fingerprint",
    "detect_and_recover_account_switch",
    "load_recorded_fingerprint",
    "overlay_messaging_backends",
    "probe_connectors",
    "record_fingerprint",
]
