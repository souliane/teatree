import logging
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from teatree.core.views._webhook_secrets import STORE_PREFIX, WebhookSecrets, WebhookSecretUnavailableError
from teatree.utils import secrets

TARGET = "repository-1"
KEY = f"{STORE_PREFIX}/{TARGET}"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@contextmanager
def fake_store(entries: dict[str, str], reads: list[str], listings: list[str]) -> Iterator[None]:
    def read_pass(key: str) -> str:
        reads.append(key)
        return entries.get(key, "")

    def pass_entry_names(prefix: str) -> frozenset[str]:
        listings.append(prefix)
        return frozenset(key.removeprefix(f"{prefix}/") for key in entries)

    with patch.multiple(secrets, read_pass=read_pass, pass_entry_names=pass_entry_names):
        yield


def test_concurrent_cold_lookups_make_one_read() -> None:
    reads: list[str] = []
    started = threading.Event()
    release = threading.Event()

    def blocking_read(key: str) -> str:
        reads.append(key)
        started.set()
        release.wait(timeout=5)
        return "s3cret"

    store = WebhookSecrets()
    with (
        patch.multiple(secrets, read_pass=blocking_read, pass_entry_names=lambda _prefix: frozenset({TARGET})),
        ThreadPoolExecutor(max_workers=4) as pool,
    ):
        futures = [pool.submit(store.secret_for, TARGET) for _ in range(4)]
        assert started.wait(timeout=5)
        time.sleep(0.2)
        release.set()
        results = [future.result(timeout=5) for future in futures]

    assert results == ["s3cret"] * 4
    assert reads == [KEY]


def test_refresh_is_allowed_once_per_window_per_target() -> None:
    clock = Clock()
    reads: list[str] = []
    store = WebhookSecrets(now=clock)
    with fake_store({KEY: "first"}, reads, []):
        assert store.secret_for(TARGET) == "first"
        assert store.refreshed_secret(TARGET) == "first"
        clock.now += 59
        assert store.refreshed_secret(TARGET) is None
        clock.now += 2
        assert store.refreshed_secret(TARGET) == "first"

    assert reads == [KEY, KEY, KEY]


def test_a_refresh_that_fails_keeps_the_cached_secret() -> None:
    store = WebhookSecrets()
    with fake_store({KEY: "first"}, [], []):
        assert store.secret_for(TARGET) == "first"
    with (
        patch.object(secrets, "read_pass", side_effect=secrets.SecretStoreError("keyring did not answer")),
        pytest.raises(WebhookSecretUnavailableError, match="keyring did not answer"),
    ):
        store.refreshed_secret(TARGET)

    assert store.secret_for(TARGET) == "first"


def test_listing_is_reused_within_its_ttl_and_renewed_after() -> None:
    clock = Clock()
    entries: dict[str, str] = {}
    listings: list[str] = []
    store = WebhookSecrets(now=clock)
    with fake_store(entries, [], listings):
        assert store.secret_for(TARGET) is None
        entries[KEY] = "added"
        clock.now += 29
        assert store.secret_for(TARGET) is None
        clock.now += 2
        assert store.secret_for(TARGET) == "added"

    assert listings == [STORE_PREFIX, STORE_PREFIX]


def test_unknown_target_warning_repeats_after_the_window_with_the_suppressed_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    store = WebhookSecrets(now=clock)
    with fake_store({}, [], []), caplog.at_level(logging.DEBUG, logger="teatree.core.views._webhook_secrets"):
        for _ in range(3):
            store.secret_for("repository-9")
        clock.now += 61
        store.secret_for("repository-9")

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "repository-9" in warnings[0]
    assert "2 similar" in warnings[1]
