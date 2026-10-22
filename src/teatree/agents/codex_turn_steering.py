"""Admission of peer and operator input into one live Codex turn."""

import asyncio
from collections.abc import Callable, Coroutine
from contextlib import suppress
from typing import Any

from teatree.agents.live_control import RejectCode, SteerRejectedError


class CodexSteerUnknownError(Exception):
    """A written steer whose admission the App Server did not confirm."""

    @classmethod
    def timed_out(cls) -> "CodexSteerUnknownError":
        return cls("Codex turn ended or admission timed out")

    @classmethod
    def answer_lost(cls) -> "CodexSteerUnknownError":
        return cls("Codex admission answer was lost")

    @classmethod
    def wrong_turn(cls) -> "CodexSteerUnknownError":
        return cls("Codex answered for another turn")


class CodexTurnSteering:
    def __init__(self) -> None:
        self._steer_turn_id = ""
        self._steer_turn_over: asyncio.Event | None = None
        self._steer_closed = False

    def turn_opened(self, turn_id: str) -> None:
        self._steer_turn_id = turn_id
        self._steer_turn_over = asyncio.Event()

    def turn_closed(self, *, session: bool = False) -> None:
        if self._steer_turn_over is not None:
            self._steer_turn_over.set()
        self._steer_turn_id = ""
        if session:
            self._steer_closed = True

    async def steer(
        self,
        text: str,
        *,
        input_id: str,
        wait: float,
        thread_id: str,
        send_request: Callable[[str, dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]],
    ) -> None:
        del input_id
        if self._steer_closed:
            raise SteerRejectedError(RejectCode.SESSION_CLOSED)
        turn_id, turn_over = self._steer_turn_id, self._steer_turn_over
        if not turn_id or turn_over is None or turn_over.is_set():
            raise SteerRejectedError(RejectCode.TURN_ENDED)
        request_task = asyncio.create_task(
            send_request(
                "turn/steer",
                {"threadId": thread_id, "expectedTurnId": turn_id, "input": [{"type": "text", "text": text}]},
            )
        )
        ended = asyncio.create_task(turn_over.wait())
        try:
            done, _ = await asyncio.wait({request_task, ended}, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
            if request_task not in done:
                raise CodexSteerUnknownError.timed_out()
            try:
                answer = request_task.result()
            except SteerRejectedError:
                raise
            except Exception as exc:
                raise CodexSteerUnknownError.answer_lost() from exc
            if answer.get("turnId") != turn_id:
                raise CodexSteerUnknownError.wrong_turn()
        finally:
            for pending in (request_task, ended):
                if not pending.done():
                    pending.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(request_task, ended, return_exceptions=True)

    @staticmethod
    async def steer_refusal(
        thread_id: str,
        turn_id: str,
        reason: str,
        send_request: Callable[[str, dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]],
    ) -> None:
        steer = {
            "threadId": thread_id,
            "expectedTurnId": turn_id,
            "input": [{"type": "text", "text": f"TeaTree PreToolUse refused the action: {reason}"}],
        }
        await asyncio.wait_for(send_request("turn/steer", steer), timeout=5)
