"""Shared route and runtime for opt-in Pydantic mailbox proofs."""

from collections.abc import Mapping

from teatree.agents.pydantic_ai_config import OpenAICompatibleLaneConfig, PydanticAiModelConfig
from teatree.agents.runner_heartbeat import HeartbeatRuntime
from teatree.agents.runner_watchdog import LoopWatchdog, TaskUsage
from teatree.llm.credentials import CredentialSource, PassSource
from teatree.llm.openai_compatible import OpenAICompatibleCredential

LIVE_ROUTE_ENTRY = "openrouter/api-key"
LIVE_ROUTE_BASE_URL = "https://openrouter.ai/api/v1"
LIVE_ROUTE_MODEL = "qwen/qwen3-235b-a22b-instruct-2507"


def resolve_live_route(
    *, source: CredentialSource | None = None, context_windows: Mapping[str, int] | None = None
) -> PydanticAiModelConfig:
    OpenAICompatibleCredential(
        sources=(source if source is not None else PassSource(),),
        pass_path_override=LIVE_ROUTE_ENTRY,
    ).resolve()
    return PydanticAiModelConfig(
        backend=OpenAICompatibleLaneConfig(
            base_url=LIVE_ROUTE_BASE_URL,
            credential_entry=LIVE_ROUTE_ENTRY,
            model=LIVE_ROUTE_MODEL,
            request_limit=6,
        ),
        max_tokens=512,
        context_windows=context_windows or {},
    )


def live_runtime(*, idle_window_seconds: float = 0) -> HeartbeatRuntime:
    return HeartbeatRuntime(
        watchdog=LoopWatchdog(max_runtime_seconds=240, max_turns=0, max_cost_usd=0.0),
        heartbeat_interval=20,
        sample_usage=lambda _task: TaskUsage(turns=0, cost_usd=0.0),
        renew_lease=lambda _task: None,
        drain_reason=lambda: "",
        idle_window_seconds=idle_window_seconds,
    )
