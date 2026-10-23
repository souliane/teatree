"""Session start/close pairing and the account check every Codex App Server transport shares."""

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Protocol

from teatree.agents.codex_app_server_errors import auth_persist_error, managed_auth_error
from teatree.agents.codex_auth_cache import CodexAuthCacheError


class StartedSession(Protocol):
    thread_id: str
    turn_id: str

    async def start(self) -> None: ...

    async def close(self) -> None: ...


@asynccontextmanager
async def opened_session(session: StartedSession) -> AsyncGenerator[None]:
    await session.start()
    try:
        yield
    finally:
        await session.close()


@asynccontextmanager
async def opened_shared_session(session: StartedSession) -> AsyncGenerator[None]:
    """A credential failure is a persist failure once a thread exists, else a missing managed login."""
    try:
        async with opened_session(session):
            yield
    except CodexAuthCacheError as exc:
        if session.thread_id:
            raise auth_persist_error(
                side_effects_started=bool(session.turn_id), agent_session_id=session.thread_id
            ) from exc
        raise managed_auth_error() from exc


def chatgpt_authenticated(account: Mapping[str, object]) -> bool:
    payload = account.get("account")
    return isinstance(payload, dict) and payload.get("type") == "chatgpt"
