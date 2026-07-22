"""The ``pydantic_ai`` in-flight session — the ``HarnessSession`` surface over an ``Agent``.

Split out of :mod:`teatree.agents.harness` (module-health LOC cap): the session adapts
pydantic_ai's streamed output into the SAME ``claude_agent_sdk`` message vocabulary every
harness backend yields, so the driver (:func:`teatree.agents.headless._collect`) never
special-cases the transport. It depends on neither the ``Harness`` protocol nor the registry
— only the message vocabulary and the Lane-B compaction policy — so it lives below the
harness module with no import cycle. Re-exported from ``teatree.agents.harness`` for
back-compat (``from teatree.agents.harness import PydanticAiHarnessSession``).
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_ai.usage import UsageLimits

from teatree.agents.lane_b.compaction import CompactionPolicy, compact_history

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.result import StreamedRunResult

#: The response-usage keys OrcaRouter (or another metered OpenAI-compatible router) may
#: carry its own per-request cost under, when pydantic_ai threads it through ``RunUsage.details``.
_ROUTER_COST_KEYS = ("cost", "cost_usd", "total_cost", "total_cost_usd")


def _router_reported_cost(run_usage: object) -> float | None:
    """The metered router's OWN reported cost from a pydantic_ai run usage, or ``None`` (#3157 E5).

    A metered OpenAI-compatible router (OrcaRouter) knows the real per-request cost; core only
    estimates it. When pydantic_ai surfaces that figure in ``RunUsage.details``, record THAT
    number (flagged not-estimated) instead of the price-table estimate. Absent (the common case
    today) → ``None``, so the estimate is used and flagged as such. Best-effort: any cost-like
    key, coerced to a non-negative float.
    """
    details = getattr(run_usage, "details", None)
    if not isinstance(details, dict):
        return None
    for key in _ROUTER_COST_KEYS:
        value = details.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
            return float(value)
    return None


def _error_turns(stream: "StreamedRunResult[None, str] | None") -> int:
    """Turns actually made when a run ERRORED, from the stream's usage — else ``1``.

    ``None`` when the error hit ``run_stream`` entry before the first request (no
    stream bound); a ``0`` request count degrades to ``1`` so a failed attempt
    always records at least the one turn it attempted.
    """
    if stream is None:
        return 1
    requests = stream.usage.requests
    return requests if requests > 0 else 1


def _tool_blocks_since(messages: "list[ModelMessage]", start: int) -> "Iterator[AssistantMessage]":
    """Yield the tool call/result blocks a turn produced, in the seam's vocabulary.

    Maps each pydantic_ai ``ToolCallPart`` produced this turn onto a
    :class:`~claude_agent_sdk.ToolUseBlock` and each ``ToolReturnPart`` /
    ``RetryPromptPart`` (a gate refusal) onto a
    :class:`~claude_agent_sdk.ToolResultBlock` (``is_error`` set for a refusal),
    each carried in its own :class:`~claude_agent_sdk.AssistantMessage`. This is
    what turns the ``pydantic_ai`` lane from text-in/text-out into a tool-emitting
    session the driver (:func:`teatree.agents.headless._collect`) sees in the same
    vocabulary the ``claude_sdk`` lane yields. *start* is the message count of the
    (compacted) seed history, so only THIS turn's messages are mapped.
    """
    for message in messages[start:]:
        if isinstance(message, ModelResponse):
            for part in message.parts:
                if isinstance(part, ToolCallPart):
                    yield AssistantMessage(
                        content=[ToolUseBlock(id=part.tool_call_id, name=part.tool_name, input=_as_input(part.args))],
                        model="",
                    )
        elif isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, ToolReturnPart):
                    yield AssistantMessage(
                        content=[ToolResultBlock(tool_use_id=part.tool_call_id, content=str(part.content))],
                        model="",
                    )
                elif isinstance(part, RetryPromptPart):
                    yield AssistantMessage(
                        content=[
                            ToolResultBlock(
                                tool_use_id=part.tool_call_id or "",
                                content=_retry_text(part),
                                is_error=True,
                            )
                        ],
                        model="",
                    )


def _as_input(args: object) -> dict[str, Any]:
    """Coerce a ``ToolCallPart.args`` (dict or JSON string) to a plain dict.

    The return feeds ``ToolUseBlock.input``, whose claude_agent_sdk contract is
    ``dict[str, Any]`` — a tool's arguments are genuinely arbitrary JSON, so the
    value type is unavoidably dynamic here.
    """
    if isinstance(args, dict):
        return {str(k): v for k, v in args.items()}
    if isinstance(args, str):
        with suppress(json.JSONDecodeError):
            parsed = json.loads(args)
            if isinstance(parsed, dict):
                return {str(k): v for k, v in parsed.items()}
    return {}


def _retry_text(part: RetryPromptPart) -> str:
    """The refusal text of a ``RetryPromptPart`` (a gate deny), as a plain string."""
    content = part.content
    return content if isinstance(content, str) else str(content)


class PydanticAiHarnessSession:
    """The ``pydantic_ai`` in-flight session — the ``HarnessSession`` surface over an ``Agent``.

    Adapts pydantic_ai's streamed output into the SAME ``claude_agent_sdk``
    message vocabulary every backend yields (module docstring), so the driver
    never special-cases the transport. ``query``/``receive_response`` are
    decoupled (one queued prompt consumed per turn) so a multi-turn conversation
    keeps ``message_history`` across calls, matching ``ClaudeSDKClient``'s
    contract — proved by :mod:`tests.teatree_agents.test_harness`.

    A provider/run error is mapped into the same TRUTHFUL terminal
    ``ResultMessage`` the claude_sdk lane yields rather than propagated raw: a
    :class:`~pydantic_ai.exceptions.ModelHTTPError` (an Anthropic 429/529 body) →
    ``is_error=True`` carrying its ``api_error_status``, a
    :class:`~pydantic_ai.exceptions.ModelAPIError` /
    :class:`~pydantic_ai.exceptions.UnexpectedModelBehavior` /
    :class:`~pydantic_ai.exceptions.ContentFilterError` → the same status-less
    shape, and a :class:`~pydantic_ai.exceptions.UsageLimitExceeded` (the run hit
    its own step cap) → ``subtype="error_max_turns"``. The driver's failure
    taxonomy keys on ``is_error``, so the park/rotate path becomes reachable and a
    programming error still propagates to a durable ``sdk_error`` FAILED.
    ``num_turns`` is the run's real ``RunUsage.requests`` count (not a hardcoded
    ``1``), and every terminal message carries the stable per-session
    :attr:`session_id`.

    ``interrupt`` cancels the pydantic_ai ``StreamedRunResult`` (stops token
    generation, closes the underlying connection, records the interrupted state
    in message history) AND the local drain task, and sets ``_interrupted`` so
    ``receive_response`` can tell "I was deliberately interrupted" apart from an
    UNRELATED external cancellation of the awaiting coroutine itself (e.g.
    :func:`headless._drive_with_heartbeat`'s ``asyncio.wait_for`` runtime
    ceiling) — awaiting a genuine ``asyncio.Task`` propagates the awaiter's own
    cancellation straight into it, so both sources raise the identical
    ``CancelledError`` at the identical ``await task`` line; only the flag
    disambiguates them. Swallowing the latter would silently report an empty
    result instead of the runtime-breach ``stuck_reason`` the watchdog contract
    requires.

    ``history`` (#2886) SEEDS ``_history`` from a prior conversation — a
    resumed park carries the rehydrated ``list[ModelMessage]`` in here so the
    FIRST ``run_stream`` on the resumed session already includes it, matching
    ``ClaudeSDKClient``'s ``--resume`` continuation contract. The
    :attr:`history` property exposes the accumulated conversation so a caller
    (:func:`headless._collect`) can persist it back out on a subsequent park.
    """

    def __init__(
        self,
        agent: Agent[None, str],
        *,
        model_name: str,
        history: "list[ModelMessage] | None" = None,
        phase: str | None = None,
        request_limit: int | None = None,
    ) -> None:
        self._agent = agent
        self._model_name = model_name
        self._history: list[ModelMessage] = list(history) if history else []
        # Compaction only applies to a phased, tool-bearing dispatch (PR-03). An
        # un-phased run stays history-identical to #2885 — a resumed thread is
        # sent verbatim, never trimmed.
        self._phase = phase
        # The per-run sequential-request cap (OrcaRouter setup plan §4 guardrail
        # #1). A positive value becomes ``UsageLimits(request_limit=...)`` on each
        # ``run_stream`` so a cheap-model maker can't drift on a long tool loop;
        # ``None``/``<= 0`` leaves the run uncapped (the ``claude_sdk`` behaviour).
        self._request_limit = request_limit
        # A stable per-session id stamped onto EVERY terminal ``ResultMessage``
        # (success and error), so the attempt recorder (:func:`headless._attempt_usage`)
        # persists a non-empty ``agent_session_id`` — the claude_sdk lane always
        # carries one; pydantic_ai has no server-side session, so teatree mints it.
        self._session_id = uuid.uuid4().hex
        self._pending_prompt: str | None = None
        self._active_task: asyncio.Task[str] | None = None
        self._active_stream: StreamedRunResult[None, str] | None = None
        self._interrupted = False

    @property
    def history(self) -> "list[ModelMessage]":
        """The accumulated conversation so far (seed + every completed turn)."""
        return self._history

    @property
    def session_id(self) -> str:
        """The stable per-session id stamped onto every terminal ``ResultMessage``."""
        return self._session_id

    async def query(self, prompt: str) -> None:
        self._pending_prompt = prompt

    async def receive_response(self) -> AsyncIterator[object]:
        if self._pending_prompt is None:
            return
        prompt, self._pending_prompt = self._pending_prompt, None
        self._interrupted = False
        # Compact the conversation the model actually sees (the ``history_processors``
        # equivalent — trim the stale middle before the turn) ONLY for a phased,
        # tool-bearing run; a short history is returned unchanged so a normal
        # phased run is byte-identical. An un-phased run sends its history
        # verbatim, so a resumed #2885 thread is never trimmed.
        sent_history = (
            compact_history(self._history, policy=CompactionPolicy.for_phase(self._phase))
            if self._phase
            else self._history
        )
        stream_for_usage: StreamedRunResult[None, str] | None = None
        try:
            async with self._agent.run_stream(
                prompt, message_history=sent_history, usage_limits=self._usage_limits()
            ) as stream:
                stream_for_usage = stream
                self._active_stream = stream
                task = asyncio.ensure_future(self._drain(stream))
                self._active_task = task
                try:
                    text = await task
                except asyncio.CancelledError:
                    if self._interrupted:
                        return
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    raise
                finally:
                    self._active_task = None
                    self._active_stream = None
                all_messages = stream.all_messages()
                self._history = all_messages
                run_usage = stream.usage
        except UsageLimitExceeded as exc:
            # The run hit its OWN per-run request cap (``_request_limit``) — a genuine
            # FAILED, NOT a park: its message names no rate/usage-limit phrase, so
            # ``classify_limit`` never mistakes it for a recoverable window.
            yield self._error_result(exc, subtype="error_max_turns", num_turns=_error_turns(stream_for_usage))
            return
        except ModelHTTPError as exc:
            yield self._error_result(
                exc,
                subtype="error_during_execution",
                num_turns=_error_turns(stream_for_usage),
                api_error_status=exc.status_code,
            )
            return
        except (ModelAPIError, UnexpectedModelBehavior) as exc:
            # A provider/run error with no HTTP status (``ContentFilterError`` is a
            # ``UnexpectedModelBehavior``, ``ModelHTTPError`` is caught above).
            yield self._error_result(exc, subtype="error_during_execution", num_turns=_error_turns(stream_for_usage))
            return
        # Surface this turn's tool calls/results in the seam's tool-block
        # vocabulary BEFORE the final text, so a tool-emitting Lane-B session
        # looks to the driver exactly like the claude_sdk lane's.
        for tool_message in _tool_blocks_since(all_messages, len(sent_history)):
            yield tool_message
        yield AssistantMessage(content=[TextBlock(text=text)], model=self._model_name)
        yield ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=run_usage.requests,
            session_id=self._session_id,
            # #3157 E5: pass the metered router's OWN reported cost through when it surfaces
            # one, so the attempt records the real figure (flagged not-estimated) instead of
            # the price-table guess; ``None`` (the common case) falls back to the estimate.
            total_cost_usd=_router_reported_cost(run_usage),
            usage={
                "input_tokens": run_usage.input_tokens,
                "output_tokens": run_usage.output_tokens,
                "cache_read_input_tokens": run_usage.cache_read_tokens,
                "cache_creation_input_tokens": run_usage.cache_write_tokens,
            },
            result=text,
            model_usage={self._model_name: {}},
        )

    def _error_result(
        self, exc: Exception, *, subtype: str, num_turns: int, api_error_status: int | None = None
    ) -> ResultMessage:
        """A truthful terminal ``ResultMessage`` for a provider/run error (``is_error=True``).

        The SAME error-shaped envelope the claude_sdk lane yields, so the driver's
        failure taxonomy (:func:`headless._limit_match` / ``_error_result_reason``)
        keys on ``is_error`` and classifies (or fails) it without special-casing the
        transport. ``api_error_status`` carries the HTTP status for a
        :class:`~pydantic_ai.exceptions.ModelHTTPError` (rendered by
        ``_error_result_reason``), ``None`` otherwise.
        """
        return ResultMessage(
            subtype=subtype,
            duration_ms=0,
            duration_api_ms=0,
            is_error=True,
            num_turns=num_turns,
            session_id=self._session_id,
            result=str(exc),
            api_error_status=api_error_status,
            model_usage={self._model_name: {}},
        )

    def _usage_limits(self) -> UsageLimits | None:
        """The per-run step cap as pydantic_ai ``UsageLimits``, or ``None`` when uncapped.

        A positive :attr:`_request_limit` caps the model-request count per run
        (OrcaRouter setup plan §4 guardrail #1); ``None``/``<= 0`` returns ``None``
        so the run is uncapped — the shipped behaviour for a resumed #2885 thread
        opened with no cap.
        """
        if self._request_limit is not None and self._request_limit > 0:
            return UsageLimits(request_limit=self._request_limit)
        return None

    @staticmethod
    async def _drain(stream: "StreamedRunResult[None, str]") -> str:
        parts = [chunk async for chunk in stream.stream_text(delta=True)]
        return "".join(parts)

    async def interrupt(self) -> None:
        if self._active_task is None:
            return
        self._interrupted = True
        if self._active_stream is not None:
            await self._active_stream.cancel()
        self._active_task.cancel()
