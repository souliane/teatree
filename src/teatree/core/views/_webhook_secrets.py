"""Per-webhook GitHub signing secrets, read from the ``pass`` store under ``teatree/github-webhook/<type>-<id>``.

Unauthenticated requests choose the target, so each store cost they can trigger is bounded: an unlisted target
costs a cached directory listing, a listed one at most one decrypt per refresh window.
"""

import logging
import threading
import time
from collections.abc import Callable
from functools import cache

from teatree.utils import secrets as secret_store

logger = logging.getLogger(__name__)

STORE_PREFIX = "teatree/github-webhook"
LISTING_TTL_SECONDS = 30.0
REFRESH_WINDOW_SECONDS = 60.0
UNKNOWN_TARGET_WARNING_WINDOW_SECONDS = 60.0


class WebhookSecretUnavailableError(RuntimeError):
    @classmethod
    def store_failed(cls, error: secret_store.SecretStoreError) -> "WebhookSecretUnavailableError":
        return cls(str(error))

    @classmethod
    def empty(cls, target: str) -> "WebhookSecretUnavailableError":
        return cls(f"{STORE_PREFIX}/{target} is listed in the `pass` store but reads back empty")


class WebhookSecrets:
    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._listed: frozenset[str] = frozenset()
        self._listed_at: float | None = None
        self._secrets: dict[str, str] = {}
        self._read_locks: dict[str, threading.Lock] = {}
        self._refreshed_at: dict[str, float] = {}
        self._unknown_warned_at: float | None = None
        self._unknown_suppressed = 0

    def secret_for(self, target: str) -> str | None:
        """The signing secret for *target*, or ``None`` when the store has no entry for it."""
        with self._lock:
            cached = self._secrets.get(target)
        if cached is not None:
            return cached
        if not self._is_listed(target):
            self._note_unknown(target)
            return None
        with self._read_lock(target):
            with self._lock:
                cached = self._secrets.get(target)
            return cached if cached is not None else self._remember(target, self._read(target))

    def refreshed_secret(self, target: str) -> str | None:
        """Re-read *target* after a signature mismatch, or ``None`` while its refresh window is still open."""
        with self._lock:
            last = self._refreshed_at.get(target)
            if last is not None and self._now() - last < REFRESH_WINDOW_SECONDS:
                return None
            self._refreshed_at[target] = self._now()
        with self._read_lock(target):
            return self._remember(target, self._read(target))

    def _is_listed(self, target: str) -> bool:
        with self._lock:
            if self._listed_at is None or self._now() - self._listed_at >= LISTING_TTL_SECONDS:
                try:
                    self._listed = secret_store.pass_entry_names(STORE_PREFIX)
                except secret_store.SecretStoreError as exc:
                    raise WebhookSecretUnavailableError.store_failed(exc) from exc
                self._listed_at = self._now()
            return target in self._listed

    def _read_lock(self, target: str) -> threading.Lock:
        with self._lock:
            return self._read_locks.setdefault(target, threading.Lock())

    @staticmethod
    def _read(target: str) -> str:
        try:
            return secret_store.read_pass(f"{STORE_PREFIX}/{target}")
        except secret_store.SecretStoreError as exc:
            raise WebhookSecretUnavailableError.store_failed(exc) from exc

    def _remember(self, target: str, secret: str) -> str:
        with self._lock:
            if secret:
                self._secrets[target] = secret
            else:
                self._secrets.pop(target, None)
        if not secret:
            raise WebhookSecretUnavailableError.empty(target)
        return secret

    def _note_unknown(self, target: str) -> None:
        with self._lock:
            now = self._now()
            last = self._unknown_warned_at
            if last is not None and now - last < UNKNOWN_TARGET_WARNING_WINDOW_SECONDS:
                self._unknown_suppressed += 1
                logger.debug("GitHub webhook names unconfigured target %s", target)
                return
            suppressed, self._unknown_suppressed = self._unknown_suppressed, 0
            self._unknown_warned_at = now
        logger.warning(
            "GitHub webhook names unconfigured target %s: no %s/%s entry in the `pass` store (%d similar suppressed)",
            target,
            STORE_PREFIX,
            target,
            suppressed,
        )


@cache
def webhook_secrets() -> WebhookSecrets:
    return WebhookSecrets()


def reset_webhook_secrets() -> None:
    webhook_secrets.cache_clear()
