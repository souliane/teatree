"""The Codex credential cache stays private across hydrate, use, and persist."""

import asyncio
import base64
import fcntl
import multiprocessing
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from teatree.agents.codex_auth_cache import (
    CODEX_AUTH_PASS_ENTRY,
    CodexAuthCache,
    CodexAuthCacheError,
    decode_auth_cache,
    store_auth_cache,
    store_auth_cache_from_reader,
)


def _encoded(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _hold_process_lock(lock_path: str, acquired: Any, release: Any) -> None:
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        acquired.set()
        release.wait(timeout=5)
    finally:
        os.close(fd)


def test_default_pass_entry_is_teatree_owned() -> None:
    assert CODEX_AUTH_PASS_ENTRY == "teatree/codex/auth-json-b64"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("not base64!", "base64"),
        (_encoded(b"not json"), "JSON"),
        (_encoded(b"[]"), "JSON object"),
        (_encoded(b'{"token":"top-secret"}') + "\nsecond-line", "one base64 line"),
    ],
)
def test_decode_rejects_invalid_secret_without_exposing_it(value: str, message: str) -> None:
    with pytest.raises(CodexAuthCacheError, match=message) as raised:
        decode_auth_cache(value)

    assert "top-secret" not in str(raised.value)


def test_hydrate_writes_atomically_with_private_permissions(tmp_path: Path) -> None:
    raw = b'{"tokens":{"access_token":"opaque"}}'
    cache = CodexAuthCache(tmp_path, pass_read=lambda _: _encoded(raw))

    assert cache.hydrate().read_bytes() == raw
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(cache.auth_path.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".auth-*")) == []


def test_store_uses_guarded_backup_and_verifies_the_single_line(tmp_path: Path) -> None:
    raw = b'{"tokens":{"access_token":"opaque"}}'
    stored: dict[str, str] = {}
    messages: list[str] = []

    def write(entry: str, value: str, *, echo: Any) -> str:
        assert callable(echo)
        stored[entry] = value
        return ""

    store_auth_cache(
        raw,
        code_home=tmp_path,
        pass_read=lambda entry: stored[entry],
        pass_write_with_backup=write,
        echo=messages.append,
    )

    assert stored == {CODEX_AUTH_PASS_ENTRY: _encoded(raw)}
    assert "\n" not in stored[CODEX_AUTH_PASS_ENTRY]
    assert messages == []


def test_store_fails_when_pass_readback_does_not_match(tmp_path: Path) -> None:
    raw = b'{"tokens":{"access_token":"opaque"}}'

    with pytest.raises(CodexAuthCacheError, match="verify"):
        store_auth_cache(
            raw,
            code_home=tmp_path,
            pass_read=lambda _entry: "different",
            pass_write_with_backup=lambda _entry, _value, *, echo: "",
            echo=lambda _message: None,
        )


def test_session_skips_pass_write_when_codex_did_not_change_auth(tmp_path: Path) -> None:
    raw = b'{"tokens":{"access_token":"same"}}'
    writes: list[str] = []
    cache = CodexAuthCache(
        tmp_path,
        pass_read=lambda _: _encoded(raw),
        pass_write_with_backup=lambda _entry, value, *, echo: writes.append(value) or "",
    )

    async def run() -> None:
        async with cache.session():
            pass

    asyncio.run(run())

    assert writes == []


def test_session_persists_changed_cache_and_verifies_readback(tmp_path: Path) -> None:
    initial = b'{"tokens":{"access_token":"old"}}'
    refreshed = b'{"tokens":{"access_token":"new"}}'
    stored = {CODEX_AUTH_PASS_ENTRY: _encoded(initial)}

    def write(entry: str, value: str, *, echo: Any) -> str:
        assert callable(echo)
        stored[entry] = value
        return ""

    cache = CodexAuthCache(tmp_path, pass_read=stored.__getitem__, pass_write_with_backup=write)

    async def run() -> None:
        async with cache.session() as auth_path:
            auth_path.write_bytes(refreshed)

    asyncio.run(run())

    assert base64.b64decode(stored[CODEX_AUTH_PASS_ENTRY]) == refreshed


def test_session_restores_private_mode_after_codex_replaces_the_cache(tmp_path: Path) -> None:
    initial = b'{"tokens":{"access_token":"old"}}'
    refreshed = b'{"tokens":{"access_token":"new"}}'
    stored = {CODEX_AUTH_PASS_ENTRY: _encoded(initial)}

    def write(entry: str, value: str, *, echo: Any) -> str:
        assert callable(echo)
        stored[entry] = value
        return ""

    cache = CodexAuthCache(tmp_path, pass_read=stored.__getitem__, pass_write_with_backup=write)

    async def run() -> None:
        async with cache.session() as auth_path:
            auth_path.unlink()
            auth_path.write_bytes(refreshed)
            auth_path.chmod(0o644)

    asyncio.run(run())

    assert stat.S_IMODE(cache.auth_path.stat().st_mode) == 0o600
    assert base64.b64decode(stored[CODEX_AUTH_PASS_ENTRY]) == refreshed


def test_changed_cache_fails_when_pass_write_cannot_be_verified(tmp_path: Path) -> None:
    initial = b'{"tokens":{"access_token":"old"}}'
    cache = CodexAuthCache(
        tmp_path,
        pass_read=lambda _: _encoded(initial),
        pass_write_with_backup=lambda _entry, _value, *, echo: "",
    )

    async def run() -> None:
        async with cache.session() as auth_path:
            auth_path.write_bytes(b'{"tokens":{"access_token":"new"}}')

    with pytest.raises(CodexAuthCacheError, match="verify"):
        asyncio.run(run())


def test_process_lock_wait_does_not_block_the_asyncio_loop(tmp_path: Path) -> None:
    lock_path = tmp_path / ".auth.lock"
    context = multiprocessing.get_context("fork")
    acquired = context.Event()
    release = context.Event()
    holder = context.Process(target=_hold_process_lock, args=(str(lock_path), acquired, release))
    holder.start()
    assert acquired.wait(timeout=2)
    raw = b'{"tokens":{"access_token":"opaque"}}'
    cache = CodexAuthCache(tmp_path, pass_read=lambda _: _encoded(raw))

    async def run() -> int:
        ticks = 0

        async def enter() -> None:
            async with cache.session():
                pass

        waiter = asyncio.create_task(enter())
        while ticks < 4:
            await asyncio.sleep(0.03)
            ticks += 1
        assert not waiter.done()
        release.set()
        await asyncio.wait_for(waiter, timeout=2)
        return ticks

    try:
        assert asyncio.run(run()) == 4
    finally:
        release.set()
        holder.join(timeout=2)
        if holder.is_alive():
            holder.kill()


def test_auth_import_reads_source_after_a_running_session_releases_its_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / ".auth.lock"
    context = multiprocessing.get_context("fork")
    acquired = context.Event()
    release = context.Event()
    holder = context.Process(target=_hold_process_lock, args=(str(lock_path), acquired, release))
    holder.start()
    assert acquired.wait(timeout=2)
    stale = b'{"tokens":{"access_token":"stale"}}'
    imported = b'{"tokens":{"access_token":"rotated"}}'
    source = tmp_path / "source-auth.json"
    source.write_bytes(stale)
    stored: dict[str, str] = {}

    def write(entry: str, value: str, *, echo: Any) -> str:
        assert callable(echo)
        stored[entry] = value
        return ""

    async def run() -> None:
        task = asyncio.create_task(
            asyncio.to_thread(
                store_auth_cache_from_reader,
                source.read_bytes,
                code_home=tmp_path,
                pass_read=stored.__getitem__,
                pass_write_with_backup=write,
            )
        )
        await asyncio.sleep(0.1)
        assert not task.done()
        source.write_bytes(imported)
        release.set()
        await asyncio.wait_for(task, timeout=2)

    try:
        asyncio.run(run())
    finally:
        release.set()
        holder.join(timeout=2)
        if holder.is_alive():
            holder.kill()

    assert base64.b64decode(stored[CODEX_AUTH_PASS_ENTRY]) == imported


def test_cancelled_session_releases_lock(tmp_path: Path) -> None:
    raw = b'{"tokens":{"access_token":"opaque"}}'
    first = CodexAuthCache(tmp_path, pass_read=lambda _: _encoded(raw))
    second = CodexAuthCache(tmp_path, pass_read=lambda _: _encoded(raw))

    async def run() -> None:
        entered = asyncio.Event()

        async def hold() -> None:
            async with first.session():
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(hold())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with asyncio.timeout(1), second.session():
            pass

    asyncio.run(run())


def test_cancelling_a_process_lock_waiter_closes_its_lock_descriptor(tmp_path: Path) -> None:
    lock_path = tmp_path / ".auth.lock"
    context = multiprocessing.get_context("fork")
    acquired = context.Event()
    release = context.Event()
    holder = context.Process(target=_hold_process_lock, args=(str(lock_path), acquired, release))
    holder.start()
    assert acquired.wait(timeout=2)
    raw = b'{"tokens":{"access_token":"opaque"}}'
    cancelled = CodexAuthCache(tmp_path, pass_read=lambda _: _encoded(raw))
    successor = CodexAuthCache(tmp_path, pass_read=lambda _: _encoded(raw))

    async def run() -> None:
        async def wait_for_lock() -> None:
            async with cancelled.session():
                pytest.fail("the held process lock must not be bypassed")

        waiting = asyncio.create_task(wait_for_lock())
        await asyncio.sleep(0.1)
        assert not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        release.set()
        await asyncio.to_thread(holder.join, 2)
        async with asyncio.timeout(1), successor.session():
            pass

    try:
        asyncio.run(run())
    finally:
        release.set()
        holder.join(timeout=2)
        if holder.is_alive():
            holder.kill()
