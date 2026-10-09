"""Messaging egress helpers that sit above the raw backend transports.

Currently hosts the verified-delivery notify wrapper (#1181): a thin
resilience layer that tries the canonical ``notify_user`` path first and
falls back to a direct, round-trip-verified messaging-backend send when
the primary path does not deliver.
"""

from teatree.messaging.notify_with_fallback import NotifyResult, NotifyTransport, notify_with_fallback

__all__ = ["NotifyResult", "NotifyTransport", "notify_with_fallback"]
