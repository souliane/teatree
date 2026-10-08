"""Per-webhook GitHub signing secrets, read from the ``pass`` store under ``teatree/github-webhook/<type>-<id>``.

Unauthenticated requests choose the target, so each store cost they can trigger is bounded: an unlisted target
costs a cached directory listing, and a listed one at most one read per window, whatever that read returned.
"""

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache

from teatree.utils import secrets as secret_store

logger = logging.getLogger(__name__)

STORE_PREFIX = "teatree/github-webhook"
LISTING_TTL_SECONDS = 30.0
SECRET_TTL_SECONDS = 600.0
FAILED_READ_WINDOW_SECONDS = 60.0
REFRESH_WINDOW_SECONDS = 60.0
READ_WAIT_SECONDS = 5.0
UNKNOWN_TARGET_WARNING_WINDOW_SECONDS = 60.0


class WebhookSecretUnavailableError(RuntimeError):
    @classmethod
    def store_failed(cls, error: secret_store.SecretStoreError) -> "WebhookSecretUnavailableError":
        return cls(str(error))

    @classmethod
    def empty(cls, target: str) -> "WebhookSecretUnavailableError":
        return cls(f"{STORE_PREFIX}/{target} is listed in the `pass` store but reads back empty")

    @classmethod
    def busy(cls, target: str) -> "WebhookSecretUnavailableError":
        return cls(f"a read of {STORE_PREFIX}/{target} is still in progress")


@dataclass(frozen=True, slots=True)
class _ReadOutcome:
    at: float
    secret: str = ""
    failure: str = ""

    def is_fresh(self, now: float) -> bool:
        return now - self.at < (SECRET_TTL_SECONDS if self.secret else FAILED_READ_WINDOW_SECONDS)


class WebhookSecrets:
    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._listed: frozenset[str] = frozenset()
        self._listing_failure = ""
        self._listed_at: float | None = None
        self._outcomes: dict[str, _ReadOutcome] = {}
        self._read_locks: dict[str, threading.Lock] = {}
        self._refreshed_at: dict[str, float] = {}
        self._unknown_warned_at: float | None = None
        self._unknown_suppressed = 0

    def secret_for(self, target: str) -> str | None:
        """The signing secret for *target*, or ``None`` when the store has no entry for it."""
        if (secret := self._remembered(target)) is not None:
            return secret
        if not self._is_listed(target):
            self._note_unknown(target)
            return None
        with self._read_slot(target):
            return self._remembered(target) or self._read(target)

    def refreshed_secret(self, target: str) -> str | None:
        """Re-read *target* after a signature mismatch, or ``None`` while its refresh window is still open."""
        with self._lock:
            last = self._refreshed_at.get(target)
            if last is not None and self._now() - last < REFRESH_WINDOW_SECONDS:
                return None
            self._refreshed_at[target] = self._now()
        with self._read_slot(target):
            return self._read(target)

    def _remembered(self, target: str) -> str | None:
        with self._lock:
            outcome = self._outcomes.get(target)
            if outcome is None or not outcome.is_fresh(self._now()):
                return None
        if outcome.failure:
            raise WebhookSecretUnavailableError(outcome.failure)
        return outcome.secret

    def _is_listed(self, target: str) -> bool:
        with self._lock:
            if self._listed_at is None or self._now() - self._listed_at >= LISTING_TTL_SECONDS:
                self._listed_at = self._now()
                try:
                    self._listed, self._listing_failure = secret_store.pass_entry_names(STORE_PREFIX), ""
                except secret_store.SecretStoreError as exc:
                    self._listing_failure = str(WebhookSecretUnavailableError.store_failed(exc))
                    logger.warning("GitHub webhook secrets unavailable: %s", self._listing_failure)
            failure, listed = self._listing_failure, target in self._listed
        if failure:
            raise WebhookSecretUnavailableError(failure)
        return listed

    @contextmanager
    def _read_slot(self, target: str) -> Iterator[None]:
        with self._lock:
            read_lock = self._read_locks.setdefault(target, threading.Lock())
        if not read_lock.acquire(timeout=READ_WAIT_SECONDS):
            raise WebhookSecretUnavailableError.busy(target)
        try:
            yield
        finally:
            read_lock.release()

    def _read(self, target: str) -> str:
        try:
            secret = secret_store.read_pass(f"{STORE_PREFIX}/{target}")
        except secret_store.SecretStoreError as exc:
            raise self._failed(target, WebhookSecretUnavailableError.store_failed(exc), evict=False) from exc
        if not secret:
            raise self._failed(target, WebhookSecretUnavailableError.empty(target), evict=True)
        with self._lock:
            self._outcomes[target] = _ReadOutcome(at=self._now(), secret=secret)
        return secret

    def _failed(
        self, target: str, error: WebhookSecretUnavailableError, *, evict: bool
    ) -> WebhookSecretUnavailableError:
        with self._lock:
            now = self._now()
            current = self._outcomes.get(target)
            if evict or current is None or not current.secret or not current.is_fresh(now):
                self._outcomes[target] = _ReadOutcome(at=now, failure=str(error))
        logger.warning("GitHub webhook secret unavailable: %s", error)
        return error

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
