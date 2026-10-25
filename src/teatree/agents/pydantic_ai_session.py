"""The ``pydantic_ai`` in-flight session — the ``HarnessSession`` surface over an ``Agent``.

Split out of :mod:`teatree.agents.harness` (module-health LOC cap): the session adapts
a pydantic_ai run into the SAME ``claude_agent_sdk`` message vocabulary every
harness backend yields, so the driver (:func:`teatree.agents.runner_stream._collect`) never
special-cases the transport. It depends on neither the ``Harness`` protocol nor the registry
— only the message vocabulary and the Lane-B compaction policy — so it lives below the
harness module with no import cycle. Re-exported from ``teatree.agents.harness`` for
back-compat (``from teatree.agents.harness import PydanticAiHarnessSession``).
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Final

from claude_agent_sdk import AssistantMessage, RateLimitEvent, ResultMessage, TextBlock
from pydantic_ai import Agent, CallToolsNode, ModelRequestNode
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
    RunCancelled,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import ModelResponse
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_graph import End

from teatree.agents.lane_b.compaction import LateHistoryCompactor
from teatree.agents.provider_refusal import hard_refusal_event
from teatree.agents.pydantic_ai_turn import SessionRun, StreamedToolCapture, TurnSpend, egress_block_in
from teatree.llm.anthropic_limits import EgressBlockedError

if TYPE_CHECKING:
    from pydantic_ai import AgentRun, AgentRunResult
    from pydantic_ai.messages import AgentStreamEvent, ModelMessage

#: The pydantic_ai provider/run failures a turn maps onto an ERROR ``ResultMessage``
#: instead of letting them escape as a raw exception. NARROW on purpose — it covers the
#: five provider/run errors and nothing else: :class:`~pydantic_ai.exceptions.ModelHTTPError`
#: subclasses ``ModelAPIError`` and :class:`~pydantic_ai.exceptions.ContentFilterError`
#: subclasses ``UnexpectedModelBehavior``. A programming error (``TypeError`` &c.) still
#: propagates, so a defect in teatree's own code keeps landing as the driver's durable
#: ``sdk_error`` FAILED-with-traceback rather than being laundered into a transport
#: failure; and ``asyncio.CancelledError`` is untouched (it is not an ``Exception``), so
#: the interrupt / external-cancel disambiguation in :meth:`receive_response` stays intact.
# openai 3 re-raises a request hook's own exception unwrapped, so the egress refusal is caught by type too.
_RUN_ERRORS = (ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded, EgressBlockedError)


def _is_context_overflow(exc: ModelHTTPError) -> bool:
    return exc.status_code in {400, 413} and any(
        phrase in str(exc).lower() for phrase in ("context window", "context length", "too many tokens")
    )


#: The terminal ``ResultMessage.subtype`` a max-tokens truncation is surfaced under by
#: :meth:`PydanticAiHarnessSession.receive_response`. Shared with the headless driver
#: (:func:`teatree.agents.runner._outcome_failure`) so the "detect here, alert the owner
#: there" seam keys on ONE constant rather than a literal duplicated across two modules.
MAX_TOKENS_TRUNCATION_SUBTYPE: Final[str] = "error_max_tokens"


class _MaxTokensTruncationError(RuntimeError):
    """The model response was cut off at the ``max_tokens`` ceiling (``finish_reason='length'``).

    A max-tokens stop amputates the JSON result envelope, so the run is surfaced as an ERROR
    (``error_max_tokens``) rather than a success carrying a silently-truncated result.
    """


def _hit_max_tokens(messages: "list[ModelMessage]") -> bool:
    """Whether the final ``ModelResponse`` stopped on a max-tokens (``'length'``) finish reason.

    Reads pydantic_ai's OWN native, normalized terminal signal — the typed
    :attr:`~pydantic_ai.messages.ModelResponse.finish_reason`
    (:data:`~pydantic_ai.messages.FinishReason`). The Anthropic binding maps the provider
    ``stop_reason`` onto it via its ``_FINISH_REASON_MAP`` (``'max_tokens' -> 'length'``,
    ``'model_context_window_exceeded' -> 'length'``) on both the streamed and non-streamed
    paths, so ``finish_reason == 'length'`` IS the library's contract for "cut off at the
    token ceiling" — not a bespoke stop-reason scrape.

    Deliberately NOT ``UsageLimits(output_tokens_limit=…)``: that is a CUMULATIVE client-side
    output budget checked across the whole run (``check_tokens(run_usage)``, strict ``>``) that
    raises ``UsageLimitExceeded`` and never inspects the provider truncation signal. It answers
    a different question (did the run's TOTAL output exceed a budget) than a per-response
    ceiling hit, cannot be aligned to ``max_tokens`` (a later short turn would false-fire, and
    an exact-ceiling hit fails the strict ``>``), and would collide with the lane's existing
    ``UsageLimitExceeded`` → ``error_max_turns`` request-cap handling. A ``max_tokens``
    truncation itself never raises in pydantic_ai — it returns this ``ModelResponse`` — so
    ``finish_reason`` is the only faithful detector.
    """
    for message in reversed(messages):
        if isinstance(message, ModelResponse):
            return message.finish_reason == "length"
    return False


#: pydantic_ai's message when a run exhausts its OUTPUT-retry budget. Its
#: empty-or-non-actionable-response path (``_agent_graph.consume_output_retry``, called with
#: no ``error``) raises it ``from None``; every other path that raises this same text attaches
#: a cause, so a ``None`` ``__cause__`` identifies the empty-response case alone.
_OUTPUT_RETRY_EXHAUSTED_PREFIX = "Exceeded maximum output retries"


def _is_quiet_turn_end(exc: Exception, captured: "StreamedToolCapture") -> bool:
    """A model that did its work and then answered nothing — a turn END, not a provider failure.

    An Anthropic model told to emit a tool call and nothing else returns an EMPTY response
    afterwards, which pydantic_ai resubmits once and then raises on. The CLI-backed lane ends
    such a turn normally, so treating it as an error is a transport divergence that discards a
    complete trajectory. Requires captured tool calls: a run that produced NOTHING stays an
    error, so the all-empty vacuous-green guard keeps its teeth.
    """
    return (
        isinstance(exc, UnexpectedModelBehavior)
        and str(exc).startswith(_OUTPUT_RETRY_EXHAUSTED_PREFIX)
        and exc.__cause__ is None
        and captured.has_tool_calls()
    )


def _model_identity_usage(model_name: str) -> dict[str, Any]:
    """The ``model_usage`` map carrying ONLY the billed model's identity, no breakdown.

    ``ResultMessage.model_usage`` is a verbatim pass-through of the CLI's ``modelUsage``
    JSON — the SDK's parser assigns ``data.get("modelUsage")`` unvalidated, so its
    ``ModelUsage`` TypedDict documents the shape the CLI sends rather than a constructor
    contract this lane must satisfy. There is no CLI on the metered lane and pydantic_ai
    reports no per-model breakdown, so the entry is deliberately EMPTY: it exists because
    :func:`~teatree.core.billed_model.dominant_model` reads the billed model from this map's KEY.
    The authoritative figures for the turn are ``usage`` and ``total_cost_usd`` on the same
    message. Zero-filling the breakdown to satisfy the TypedDict would publish
    measured-looking zeros for cost, context window, and output cap that nothing observed.
    """
    return {model_name: {}}


def _turns_made(run_usage: RunUsage) -> int:
    """The model requests the turn actually made — never zero.

    The caller OWNS the :class:`~pydantic_ai.usage.RunUsage` it hands to
    :meth:`~pydantic_ai.agent.AbstractAgent.iter` (pydantic_ai adopts that instance as
    the run's own usage state and mutates it in place), so the count survives a run
    that raised before returning a result. A ``0`` — the provider refusing the very
    first request — degrades to ``1``: that is still one attempted turn, and recording
    zero would under-count the ``LoopWatchdog`` ceiling exactly the way the hardcoded
    ``1`` over-counted a multi-request run.
    """
    return max(run_usage.requests, 1)


class PydanticAiHarnessSession:
    """The ``pydantic_ai`` in-flight session — the ``HarnessSession`` surface over an ``Agent``.

    Adapts a pydantic_ai run into the SAME ``claude_agent_sdk``
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
    its own step cap) → ``subtype="error_max_turns"``. A run that OTHERWISE succeeds
    but whose final ``ModelResponse`` stopped on a max-tokens (``'length'``) finish is
    also mapped to an error → ``subtype="error_max_tokens"``, rather than a success
    carrying the amputated JSON envelope. The driver's failure
    taxonomy keys on ``is_error``, so the park/rotate path becomes reachable and a
    programming error still propagates to a durable ``sdk_error`` FAILED.
    ``num_turns`` is the run's real ``RunUsage.requests`` count (not a hardcoded
    ``1``), and every terminal message carries the stable per-session
    :attr:`session_id`.

    ``interrupt`` cancels the active agent run and retains its completed message
    history. It sets ``_interrupted`` so ``receive_response`` can tell "I was deliberately
    interrupted" apart from an UNRELATED external cancellation of the awaiting
    coroutine itself (e.g. :func:`headless._drive_with_heartbeat`'s
    ``asyncio.wait_for`` runtime ceiling) — awaiting a genuine ``asyncio.Task``
    propagates the awaiter's own cancellation straight into it, so both sources
    raise the identical ``CancelledError`` at the identical ``await task`` line;
    only the flag disambiguates them. Swallowing the latter would silently report
    an empty result instead of the runtime-breach ``stuck_reason`` the watchdog
    contract requires.

    ``history`` (#2886) SEEDS ``_history`` from a prior conversation — a
    resumed park carries the rehydrated ``list[ModelMessage]`` in here so the
    FIRST run on the resumed session already includes it, matching
    ``ClaudeSDKClient``'s ``--resume`` continuation contract. The
    :attr:`history` property exposes the accumulated conversation so a caller
    (:func:`headless._collect`) can persist it back out on a subsequent park.

    The terminal ``ResultMessage`` is TRUTHFUL about the turn — that is what makes
    the driver's transport-agnostic failure taxonomy work on this lane at all. It
    keys entirely on ``is_error``, so a session that always reported ``success``
    made a bad run indistinguishable from a good one: a provider throttle escaped as
    a raw exception and landed as an ``sdk_error`` traceback, ``park_or_rotate_on_limit``
    unreachable. :meth:`receive_response` maps the provider/run failures
    (:data:`_RUN_ERRORS`) onto an error result carrying the provider's own text, and
    reports the run's real request count and this session's own correlation handle.
    """

    def __init__(
        self,
        agent: Agent[None, str],
        *,
        model_name: str,
        history: "list[ModelMessage] | None" = None,
        phase: str | None = None,
        run: SessionRun | None = None,
    ) -> None:
        self._agent = agent
        self._model_name = model_name
        self._history: list[ModelMessage] = list(history) if history else []
        # Compaction only applies to a phased, tool-bearing dispatch (PR-03). An
        # un-phased run stays history-identical to #2885 — a resumed thread is
        # sent verbatim, never trimmed.
        self._phase = phase
        self._compactor: LateHistoryCompactor | None = None
        # A stable per-session id stamped onto EVERY terminal ``ResultMessage``
        # (success and error), so the attempt recorder (:func:`headless._attempt_usage`)
        # persists a non-empty ``agent_session_id`` — the claude_sdk lane always
        # carries one; pydantic_ai has no server-side session, so teatree mints it.
        # A harness mints it before building the provider, so the router is keyed on the same id.
        self._run = run or SessionRun.start()
        self._session_id = self._run.session_id
        self._event_observer: Callable[[AgentStreamEvent], None] | None = None
        self._input_observer: Callable[[AgentStreamEvent], None] | None = None
        self._hook_events: Sequence[object] | None = None
        self._emitted_hook_events = 0
        # A positive cap becomes ``UsageLimits(request_limit=...)`` on each run so a cheap-model
        # maker can't drift on a long tool loop.
        self._request_limit = self._run.request_limit
        self._pending_prompt: str | None = None
        self._active_task: asyncio.Task[AgentRunResult[str]] | None = None
        self._active_run: AgentRun[None, str] | None = None
        self._interrupted = False

    def observe_transport_hooks(
        self,
        observer: "Callable[[AgentStreamEvent], None]",
        events: "Sequence[object]",
    ) -> None:
        """Attach an optional transport's streamed-event observer and hook ledger."""
        self._event_observer = observer
        self._hook_events = events

    def set_compactor(self, compactor: LateHistoryCompactor) -> None:
        self._compactor = compactor

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
        """Drive one queued turn TO COMPLETION, yielding it in the ``claude_agent_sdk`` vocabulary.

        Driven by :meth:`~pydantic_ai.agent.AbstractAgent.iter` through every graph node,
        streaming model requests and tool events through :class:`StreamedToolCapture`.
        NOT ``run_stream``, which pydantic_ai documents as
        single-shot: it "will consider the first output matching the ``output_type`` to
        be the final output, [...] stop running the agent graph and [...] not execute any
        tool calls made by the model after this 'final' output". This lane's
        ``output_type`` is ``str``, and pydantic_ai raises its ``FinalResultEvent`` on the
        FIRST ``TextPart`` of a response when text output is allowed
        (``models._get_final_result_event``) — so under ``run_stream`` a model that opened
        with one sentence of prose before calling a tool ended the whole run at one model
        request, its tool results never fed back. That is the shape every Anthropic model
        emits, so a coding dispatch could not act: it returned its own preamble as the
        result and the worktree stayed untouched. ``iter`` advances the graph until the
        model itself stops calling tools, which is the ``ClaudeSDKClient`` contract this
        seam exists to match.

        A provider/run failure (:data:`_RUN_ERRORS`) ends the turn with an
        ``is_error`` :class:`~claude_agent_sdk.ResultMessage` instead of escaping as a
        raw exception, so the driver's own taxonomy —
        :func:`teatree.agents.runner_failure_taxonomy.limit_match` → ``park_or_rotate_on_limit``,
        :func:`teatree.agents.runner_failure_taxonomy.error_result_reason` → ``_record_failure`` —
        fires on this lane exactly as it does on the ``claude_sdk`` one, with no
        driver change and no transport special-casing.
        """
        if self._pending_prompt is None:
            return
        prompt, self._pending_prompt = self._pending_prompt, None
        self._interrupted = False
        sent_history = self._history
        # Owned by THIS caller rather than read off the result, because a run that
        # raises never returns one — pydantic_ai adopts this instance as the run's
        # usage state and mutates it in place, so the error paths below still report
        # the requests the failed turn actually made.
        run_usage = RunUsage()
        captured = StreamedToolCapture(self._observe_event)
        first_request = len(self._run.usage_tee.requests)
        try:
            run_result = await self._run_with_overflow_retry(prompt, sent_history, run_usage, captured)
        except _RUN_ERRORS as exc:
            spend = self._spend(run_usage, first_request, captured)
            for hook_event in self._new_hook_events():
                yield hook_event
            for message in self._failed_turn_messages(exc, captured=captured, run_usage=run_usage, spend=spend):
                yield message
            return
        if run_result is None:
            return
        all_messages = run_result.all_messages()
        self._history = all_messages
        text = run_result.output
        for hook_event in self._new_hook_events():
            yield hook_event
        if _hit_max_tokens(all_messages):
            yield self._error_result(
                _MaxTokensTruncationError(
                    "the pydantic_ai response was truncated at the max_tokens ceiling "
                    "(finish_reason='length'); the result envelope is incomplete"
                ),
                subtype=MAX_TOKENS_TRUNCATION_SUBTYPE,
                num_turns=run_usage.requests,
                spend=self._spend(run_usage, first_request, captured),
            )
            return
        # Surface this turn's tool calls/results in the seam's tool-block
        # vocabulary BEFORE the final text, so a tool-emitting Lane-B session
        # looks to the driver exactly like the claude_sdk lane's.
        for tool_message in captured.blocks():
            yield tool_message
        yield AssistantMessage(content=[TextBlock(text=text)], model=self._model_name)
        spend = self._spend(run_usage, first_request, captured)
        yield ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            # pydantic_ai's canonical per-run model-request counter — the same
            # per-attempt semantics ``TaskUsage.for_task`` sums and the ``LoopWatchdog``
            # turn ceiling evaluates. A hardcoded 1 left a runaway session unbounded.
            num_turns=run_usage.requests,
            session_id=self._session_id,
            # #3157 E5: the metered router's OWN reported cost, so the attempt records the real
            # figure (flagged not-estimated); ``None`` falls back to the price-table estimate.
            total_cost_usd=spend.cost_usd,
            usage=spend.usage,
            result=text,
            model_usage=_model_identity_usage(spend.model),
        )

    async def _await_run(
        self, prompt: str, history: "list[ModelMessage]", usage: RunUsage, captured: StreamedToolCapture
    ) -> "AgentRunResult[str] | None":
        task = asyncio.create_task(self._drive_run(prompt, history, usage, captured))
        self._active_task = task
        try:
            return await task
        except RunCancelled as exc:
            if not self._interrupted:
                raise
            self._history = exc.all_messages()
            return None
        except asyncio.CancelledError:
            if self._interrupted:
                return None
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise
        finally:
            self._active_task = None

    async def _run_with_overflow_retry(
        self, prompt: str, history: "list[ModelMessage]", usage: RunUsage, captured: StreamedToolCapture
    ) -> "AgentRunResult[str] | None":
        try:
            return await self._await_run(prompt, history, usage, captured)
        except ModelHTTPError as exc:
            if self._compactor is None or not _is_context_overflow(exc):
                raise
            self._compactor.force_next()
            return await self._await_run("Continue from the last completed step.", self._history, usage, captured)

    def _observe_event(self, event: "AgentStreamEvent") -> None:
        if self._event_observer is not None:
            self._event_observer(event)
        if self._input_observer is not None:
            self._input_observer(event)

    async def _drive_run(
        self, prompt: str, history: "list[ModelMessage]", usage: RunUsage, captured: StreamedToolCapture
    ) -> "AgentRunResult[str]":
        async with self._agent.iter(
            prompt, message_history=history, usage_limits=self._usage_limits(), usage=usage
        ) as run:
            self._active_run = run
            try:
                node = run.next_node
                while not isinstance(node, End):
                    if isinstance(node, ModelRequestNode):
                        async with node.stream(run.ctx) as stream:
                            await captured.consume(stream)
                    elif isinstance(node, CallToolsNode):
                        async with node.stream(run.ctx) as events:
                            await captured.consume(events)
                    node = await run.next(node)
                result = run.result
                if result is None:
                    msg = "agent run ended without a result"
                    raise RuntimeError(msg)
            except _RUN_ERRORS:
                self._history = run.all_messages()
                raise
            finally:
                self._active_run = None
            return result

    def _new_hook_events(self) -> "Iterator[object]":
        """Yield hook events appended by an optional transport adapter exactly once."""
        if self._hook_events is None:
            return
        pending = self._hook_events[self._emitted_hook_events :]
        self._emitted_hook_events += len(pending)
        yield from pending

    def _failed_turn_messages(
        self, exc: Exception, *, captured: StreamedToolCapture, run_usage: RunUsage, spend: TurnSpend
    ) -> "Iterator[AssistantMessage | RateLimitEvent | ResultMessage]":
        """This turn's recovered trajectory, then the terminal message its failure maps to.

        The captured tool blocks lead on EVERY path: a run that raises returns no result,
        so the streamed capture is the only record of what the model did before it failed.
        A hard 401/403 refusal also yields its rejected window ahead of the result (#4816).
        """
        yield from captured.blocks()
        num_turns = _turns_made(run_usage)
        if (egress := egress_block_in(exc)) is not None:
            yield self._error_result(egress, subtype="error_during_execution", num_turns=num_turns, spend=spend)
        elif isinstance(exc, UsageLimitExceeded):
            # The run hit its OWN per-run request cap (``_request_limit``) — a genuine
            # FAILED, NOT a park: readers key on the subtype, because the message links
            # pydantic_ai's "usage limits" docs, which ``classify_limit`` would match.
            yield self._error_result(exc, subtype="error_max_turns", num_turns=num_turns, spend=spend)
        elif isinstance(exc, ModelHTTPError):
            if (refusal := hard_refusal_event(exc, session_id=self._session_id)) is not None:
                yield refusal
            yield self._error_result(
                exc,
                subtype="error_during_execution",
                num_turns=num_turns,
                spend=spend,
                api_error_status=exc.status_code,
            )
        elif _is_quiet_turn_end(exc, captured):
            yield AssistantMessage(content=[TextBlock(text="")], model=self._model_name)
            yield self._quiet_turn_result(num_turns=num_turns, spend=spend)
        else:
            # A provider/run error with no HTTP status (``ContentFilterError`` is a
            # ``UnexpectedModelBehavior``; ``ModelHTTPError`` is handled above).
            yield self._error_result(exc, subtype="error_during_execution", num_turns=num_turns, spend=spend)

    def _quiet_turn_result(self, *, num_turns: int, spend: TurnSpend) -> ResultMessage:
        """The terminal message for a turn the model ended without a final text output.

        Success-shaped because the work happened — the tool calls are yielded ahead of it —
        and the empty ``result`` is the truthful record that no closing text came back.
        """
        return ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=num_turns,
            session_id=self._session_id,
            total_cost_usd=spend.cost_usd,
            usage=spend.usage,
            result="",
            model_usage=_model_identity_usage(spend.model),
        )

    def _error_result(
        self, exc: Exception, *, subtype: str, num_turns: int, spend: TurnSpend, api_error_status: int | None = None
    ) -> ResultMessage:
        """A truthful terminal ``ResultMessage`` for a provider/run error (``is_error=True``).

        The SAME error-shaped envelope the claude_sdk lane yields, so the driver's
        failure taxonomy (:mod:`teatree.agents.runner_failure_taxonomy`)
        keys on ``is_error`` and classifies (or fails) it without special-casing the
        transport. ``api_error_status`` carries the HTTP status for a
        :class:`~pydantic_ai.exceptions.ModelHTTPError` (rendered by
        ``error_result_reason``), ``None`` otherwise. A failed turn still billed what it used, so it
        reports usage and cost like a successful one.
        """
        return ResultMessage(
            subtype=subtype,
            duration_ms=0,
            duration_api_ms=0,
            is_error=True,
            num_turns=num_turns,
            session_id=self._session_id,
            total_cost_usd=spend.cost_usd,
            usage=spend.usage,
            result=str(exc),
            api_error_status=api_error_status,
            model_usage=_model_identity_usage(spend.model),
        )

    def _spend(self, run_usage: RunUsage, first_request: int, captured: StreamedToolCapture) -> TurnSpend:
        return TurnSpend.measure(
            run_usage,
            self._run.usage_tee.requests[first_request:],
            requested_model=self._model_name,
            trajectory=captured.trajectory(),
        )

    def _usage_limits(self) -> UsageLimits | None:
        """The per-run step cap as pydantic_ai ``UsageLimits``, or ``None`` when uncapped.

        A positive :attr:`_request_limit` caps the model-request count per run
        (the metered-lane guardrail); ``None``/``<= 0`` returns ``None``
        so the run is uncapped — the shipped behaviour for a resumed #2885 thread
        opened with no cap.
        """
        if self._request_limit is not None and self._request_limit > 0:
            return UsageLimits(request_limit=self._request_limit)
        return None

    async def interrupt(self) -> None:
        """Abort the in-flight run, marking the abort as deliberate.

        The active run snapshots completed messages when cancelled. ``_interrupted`` is what
        :meth:`receive_response` reads to tell this abort apart from an unrelated
        external cancellation of the awaiting coroutine (class docstring).
        """
        if self._active_task is None:
            return
        self._interrupted = True
        if self._active_run is not None:
            self._active_run.cancel()
        else:
            self._active_task.cancel()
