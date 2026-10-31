"""Admission of peer and operator input into one live Codex turn."""

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from teatree.agents.harness_registry import HarnessFallbackError, HarnessFallbackKind
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
    _steer_turn_id = ""
    _steer_turn_over: asyncio.Event | None = None
    _steer_closed = False
    thread_id: str

    def _turn_opened(self, turn_id: str) -> None:
        self._steer_turn_id = turn_id
        self._steer_turn_over = asyncio.Event()

    def _turn_closed(self, *, session: bool = False) -> None:
        if self._steer_turn_over is not None:
            self._steer_turn_over.set()
        self._steer_turn_id = ""
        if session:
            self._steer_closed = True

    async def steer(self, text: str, *, input_id: str, wait: float) -> None:
        del input_id
        if self._steer_closed:
            raise SteerRejectedError(RejectCode.SESSION_CLOSED)
        turn_id, turn_over = self._steer_turn_id, self._steer_turn_over
        if not turn_id or turn_over is None or turn_over.is_set():
            raise SteerRejectedError(RejectCode.TURN_ENDED)
        request = asyncio.create_task(
            self._request(
                "turn/steer",
                {"threadId": self.thread_id, "expectedTurnId": turn_id, "input": [{"type": "text", "text": text}]},
            )
        )
        ended = asyncio.create_task(turn_over.wait())
        try:
            done, _ = await asyncio.wait({request, ended}, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
            if request not in done:
                raise CodexSteerUnknownError.timed_out()
            try:
                answer = request.result()
            except SteerRejectedError:
                raise
            except Exception as exc:
                raise CodexSteerUnknownError.answer_lost() from exc
            if answer.get("turnId") != turn_id:
                raise CodexSteerUnknownError.wrong_turn()
        finally:
            for pending in (request, ended):
                if not pending.done():
                    pending.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(request, ended, return_exceptions=True)

    async def follow_up(self, prompt: str) -> None:
        if self._steer_closed:
            raise SteerRejectedError(RejectCode.SESSION_CLOSED)
        if self._steer_turn_id:
            raise SteerRejectedError(RejectCode.NOT_IDLE)
        try:
            await self.query(prompt)
        except HarnessFallbackError as exc:
            if exc.kind is HarnessFallbackKind.TRANSPORT and not exc.side_effects_started:
                raise SteerRejectedError(RejectCode.SESSION_CLOSED) from exc
            raise

    async def _steer_refusal(self, thread_id: str, turn_id: str, reason: str) -> None:
        steer = {
            "threadId": thread_id,
            "expectedTurnId": turn_id,
            "input": [{"type": "text", "text": f"TeaTree PreToolUse refused the action: {reason}"}],
        }
        await asyncio.wait_for(self._request("turn/steer", steer), timeout=5)

    if TYPE_CHECKING:

        async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...

        async def query(self, prompt: str) -> None: ...
