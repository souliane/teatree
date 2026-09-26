"""One Codex App Server and credential writer for concurrent worker tasks."""

import asyncio
import atexit
import os
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, suppress
from pathlib import Path
from typing import Any, Protocol, TypeVar

from teatree.agents.codex_app_server import CodexAppServerSession, _StreamFailure, _thread_params
from teatree.agents.codex_app_server_errors import transport_error
from teatree.agents.codex_app_server_options import CodexAppServerError, CodexAppServerOptions
from teatree.agents.codex_auth_cache import CodexAuthCache
from teatree.agents.harness_registry import HarnessFallbackError

_T = TypeVar("_T")
_IDLE_SECONDS = 5.0
type AppServerPayload = dict[str, Any]


class _AuthCache(Protocol):
    def session(self) -> AbstractAsyncContextManager[Path]: ...

    def persist(self) -> None: ...


class SharedCodexAppServer:
    """A worker-local App Server with multiple independent Codex thread queues."""

    def __init__(
        self,
        *,
        code_home: Path,
        cache: _AuthCache | None = None,
        command: Sequence[str] | None = None,
        process_env: Mapping[str, str] | None = None,
        idle_seconds: float = _IDLE_SECONDS,
    ) -> None:
        self.code_home = code_home
        self.cache = cache or CodexAuthCache(code_home)
        self.command = command
        self.process_env = process_env
        self.idle_seconds = idle_seconds
        self._start_lock = threading.Lock()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._startup_error: BaseException | None = None
        self._transport_failed = threading.Event()
        self._retiring = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._transport: CodexAppServerSession | None = None
        self._events: dict[str, asyncio.Queue[AppServerPayload | _StreamFailure]] = {}
        self._persist_lock = asyncio.Lock()
        self._session_state_lock = asyncio.Lock()
        self._idle_task: asyncio.Task[None] | None = None
        self._opens_in_flight = 0
        self._opens_lock = threading.Lock()

    def _ensure_started(self, options: CodexAppServerOptions) -> None:
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    args=(options,),
                    name="t3-codex-app-server",
                    daemon=True,
                )
                self._thread.start()
        if not self._ready.wait(timeout=30):
            msg = "Shared Codex App Server did not start"
            raise CodexAppServerError(msg)
        if self._startup_error is not None:
            if isinstance(self._startup_error, Exception):
                raise self._startup_error
            msg = "Shared Codex App Server failed to start"
            raise CodexAppServerError(msg) from self._startup_error

    def _run(self, options: CodexAppServerOptions) -> None:
        try:
            asyncio.run(self._serve(options))
        except Exception as exc:  # noqa: BLE001 - propagate worker startup failure
            self._startup_error = exc
            self._ready.set()

    async def _serve(self, options: CodexAppServerOptions) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with self.cache.session():
            transport = CodexAppServerSession(
                options,
                resume=None,
                code_home=self.code_home,
                command=self.command,
                process_env=self.process_env,
            )
            await transport.start(open_thread=False)
            self._transport = transport
            dispatch = asyncio.create_task(self._dispatch_events())
            self._ready.set()
            self._arm_idle_if_unused()
            try:
                await self._stop.wait()
            finally:
                if self._idle_task is not None:
                    self._idle_task.cancel()
                dispatch.cancel()
                with suppress(asyncio.CancelledError):
                    await dispatch
                async with self._persist_lock:
                    await transport.close()

    async def _dispatch_events(self) -> None:
        transport = self._required_transport()
        while True:
            event = await transport.next_transport_event()
            if isinstance(event, _StreamFailure):
                self._transport_failed.set()
                self._retiring.set()
                for queue in self._events.values():
                    queue.put_nowait(event)
                if self._stop is not None:
                    self._stop.set()
                return
            params = event.get("params")
            thread_id = params.get("threadId") if isinstance(params, dict) else None
            queue = self._events.get(thread_id) if isinstance(thread_id, str) else None
            if queue is not None:
                queue.put_nowait(event)

    def _required_transport(self) -> CodexAppServerSession:
        if self._transport is None:
            raise CodexAppServerError.not_running()
        return self._transport

    async def _call(self, options: CodexAppServerOptions, operation: Callable[[], Awaitable[_T]]) -> _T:
        await asyncio.to_thread(self._ensure_started, options)
        loop = self._loop
        if loop is None or loop.is_closed():
            stage = "shared worker"
            raise transport_error(stage)

        async def invoke() -> _T:
            return await operation()

        coroutine = invoke()
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except RuntimeError as exc:
            coroutine.close()
            stage = "shared worker"
            raise transport_error(stage) from exc
        return await asyncio.wrap_future(future)

    async def open_session(self, options: CodexAppServerOptions, resume: str | None) -> tuple[str, str]:
        async def open_on_owner() -> tuple[str, str]:
            async with self._session_state_lock:
                if self._retiring.is_set():
                    raise CodexAppServerError.stopped()
                if self._idle_task is not None:
                    self._idle_task.cancel()
                    self._idle_task = None
                method = "thread/resume" if resume else "thread/start"
                params = _thread_params(options)
                if resume:
                    params = {"threadId": resume, **params}
                response = await self._required_transport().request_protocol(method, params)
                thread = response.get("thread")
                thread_id = str(thread.get("id", "")) if isinstance(thread, dict) else ""
                if not thread_id:
                    raise CodexAppServerError.missing_thread_id()
                if thread_id in self._events:
                    msg = f"Codex thread {thread_id!r} is already active in this worker"
                    raise CodexAppServerError(msg)
                self._events[thread_id] = asyncio.Queue()
                model = str(response.get("model") or options.core.model or "codex")
                return thread_id, model

        with self._opens_lock:
            self._opens_in_flight += 1
        try:
            return await self._call(options, open_on_owner)
        finally:
            with self._opens_lock:
                self._opens_in_flight -= 1
            self._arm_idle_on_owner()

    async def request(self, options: CodexAppServerOptions, method: str, params: AppServerPayload) -> AppServerPayload:
        async def request_on_owner() -> AppServerPayload:
            return await self._required_transport().request_protocol(method, params)

        return await self._call(options, request_on_owner)

    async def next_event(self, options: CodexAppServerOptions, thread_id: str) -> AppServerPayload:
        async def next_on_owner() -> AppServerPayload:
            queue = self._events.get(thread_id)
            if queue is None:
                raise CodexAppServerError.stopped()
            event = await queue.get()
            if isinstance(event, _StreamFailure):
                raise event.error
            return event

        return await self._call(options, next_on_owner)

    async def close_session(self, options: CodexAppServerOptions, thread_id: str) -> None:
        if self._loop is None or self._loop.is_closed() or self._thread is None or not self._thread.is_alive():
            return

        async def close_on_owner() -> None:
            async with self._session_state_lock:
                queue = self._events.pop(thread_id, None)
                if queue is not None:
                    queue.put_nowait(_StreamFailure(CodexAppServerError.stopped()))
                try:
                    if not self._retiring.is_set():
                        async with self._persist_lock:
                            if not self._retiring.is_set():
                                await self._persist_until_finished()
                finally:
                    # Even a failed pass write must release the idle credential lease.
                    self._arm_idle_if_unused()

        await self._call(options, close_on_owner)

    async def _persist_until_finished(self) -> None:
        # Cancelling the caller cannot release the asyncio lock while to_thread still writes.
        task = asyncio.create_task(asyncio.to_thread(self.cache.persist))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _arm_idle_on_owner(self) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(self._arm_idle_if_unused)

    def _arm_idle_if_unused(self) -> None:
        if not self._events and self._idle_task is None and not self._retiring.is_set():
            self._idle_task = asyncio.create_task(self._stop_when_idle())

    async def _stop_when_idle(self) -> None:
        await asyncio.sleep(self.idle_seconds)
        async with self._session_state_lock:
            with self._opens_lock:
                unused = not self._events and not self._opens_in_flight
            if unused and self._stop is not None:
                self._retiring.set()
                self._stop.set()
            else:
                self._idle_task = None

    def close(self) -> None:
        loop, stop, thread = self._loop, self._stop, self._thread
        if loop is not None and not loop.is_closed() and stop is not None and thread is not None and thread.is_alive():
            loop.call_soon_threadsafe(stop.set)
            thread.join(timeout=10)

    @property
    def failed(self) -> bool:
        thread = self._thread
        return (
            self._startup_error is not None
            or self._transport_failed.is_set()
            or self._retiring.is_set()
            or (thread is not None and not thread.is_alive())
        )

    @property
    def retiring(self) -> bool:
        return self._retiring.is_set()

    @property
    def stopped(self) -> bool:
        return self._thread is None or not self._thread.is_alive()


class SharedCodexSession(CodexAppServerSession):
    def __init__(self, options: CodexAppServerOptions, *, manager: SharedCodexAppServer, resume: str | None) -> None:
        super().__init__(options, resume=resume, code_home=manager.code_home)
        self.manager = manager
        self._closed = False

    async def start(self, *, open_thread: bool = True) -> None:
        del open_thread
        if self.manager.retiring:
            self.manager = shared_codex_app_server(self.manager.code_home)
        try:
            self.thread_id, self.model = await self.manager.open_session(self.options, self.resume)
        except (CodexAppServerError, HarnessFallbackError):
            if not self.manager.retiring:
                raise
            self.manager = shared_codex_app_server(self.manager.code_home)
            self.thread_id, self.model = await self.manager.open_session(self.options, self.resume)

    async def _request(self, method: str, params: AppServerPayload) -> AppServerPayload:
        try:
            return await self.manager.request(self.options, method, params)
        except HarnessFallbackError as exc:
            raise HarnessFallbackError(
                str(exc),
                kind=exc.kind,
                side_effects_started=exc.side_effects_started or bool(self.turn_id),
                agent_session_id=self.thread_id,
            ) from exc

    async def _next_event(self) -> AppServerPayload:
        try:
            return await self.manager.next_event(self.options, self.thread_id)
        except HarnessFallbackError as exc:
            raise HarnessFallbackError(
                str(exc),
                kind=exc.kind,
                side_effects_started=exc.side_effects_started or bool(self.turn_id),
                agent_session_id=self.thread_id,
            ) from exc

    async def close(self) -> None:
        if self._closed:
            return
        if self.thread_id:
            await self.manager.close_session(self.options, self.thread_id)
        self._closed = True


_managers: dict[tuple[int, Path], SharedCodexAppServer] = {}
_managers_lock = threading.Lock()


def shared_codex_app_server(code_home: Path) -> SharedCodexAppServer:
    key = (os.getpid(), code_home.resolve())
    with _managers_lock:
        if key in _managers and _managers[key].failed:
            retiring = _managers[key]
            retiring.close()
            if not retiring.stopped:
                msg = "Previous Codex credential writer has not stopped"
                raise CodexAppServerError(msg)
            _managers.pop(key)
        if key not in _managers:
            _managers[key] = SharedCodexAppServer(code_home=code_home)
        return _managers[key]


def reset_shared_codex_app_servers() -> None:
    """Release credential writers and forget worker-local managers between tests."""
    with _managers_lock:
        for manager in _managers.values():
            manager.close()
            if not manager.stopped:
                msg = "Shared Codex credential writer has not stopped"
                raise CodexAppServerError(msg)
        _managers.clear()


atexit.register(reset_shared_codex_app_servers)
