"""Operational signal for a sealed store whose key cannot open its rows."""

from django.db import DatabaseError

from teatree.core.factory.health_signal import HealthSignal, SignalCollection
from teatree.core.models.sealed_secret import SealedSecret
from teatree.utils.master_key import master_key_source


def secret_key_signals() -> SignalCollection:
    if not SealedSecret.objects.exists():
        return SignalCollection()
    try:
        health = SealedSecret.objects.key_health(master_key_source())
    except (DatabaseError, OSError, ValueError) as exc:
        return SignalCollection(
            (
                HealthSignal(
                    fingerprint="secret-key-unavailable",
                    severity="critical",
                    kind="secret_key",
                    summary=f"sealed secret key source could not be checked: {type(exc).__name__}",
                ),
            )
        )
    if health.healthy:
        return SignalCollection()
    return SignalCollection(
        (
            HealthSignal(
                fingerprint="secret-key-unavailable",
                severity="critical",
                kind="secret_key",
                summary=f"sealed secret key {health.source}: {health.status} — {health.detail}",
            ),
        )
    )
