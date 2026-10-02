"""Safe classification of Codex provider errors for route fallback."""

from teatree.agents.codex_app_server_options import CodexAppServerError
from teatree.agents.codex_auth_cache import CODEX_AUTH_PASS_ENTRY
from teatree.agents.harness_registry import HarnessFallbackError, HarnessFallbackKind

_STRING_KINDS = {
    "unauthorized": HarnessFallbackKind.AUTH,
    "sessionBudgetExceeded": HarnessFallbackKind.QUOTA,
    "usageLimitExceeded": HarnessFallbackKind.QUOTA,
    "rateLimitExceeded": HarnessFallbackKind.QUOTA,
    "cyberPolicy": HarnessFallbackKind.ACCESS,
    "misalignmentPolicyViolation": HarnessFallbackKind.ACCESS,
    "serverOverloaded": HarnessFallbackKind.PROVIDER_5XX,
    "internalServerError": HarnessFallbackKind.PROVIDER_5XX,
}
_TRANSPORT_KEYS = frozenset(
    {
        "httpConnectionFailed",
        "responseStreamConnectionFailed",
        "responseStreamDisconnected",
        "responseTooManyFailedAttempts",
    }
)
_HTTP_STATUS_KINDS = {
    401: HarnessFallbackKind.AUTH,
    403: HarnessFallbackKind.ACCESS,
    429: HarnessFallbackKind.QUOTA,
}
_SERVER_ERROR_STATUS_CLASS = 5


def request_error(
    method: str,
    payload: object,
    *,
    side_effects_started: bool = False,
    agent_session_id: str = "",
) -> CodexAppServerError | HarnessFallbackError:
    """Classify a JSON-RPC error without copying its server-controlled message."""
    info = _codex_error_info(payload)
    fallback = provider_fallback(
        info,
        context=method,
        side_effects_started=side_effects_started,
        agent_session_id=agent_session_id,
    )
    return fallback or CodexAppServerError.refused_request(method)


def turn_error(
    payload: object,
    *,
    side_effects_started: bool,
    agent_session_id: str,
) -> CodexAppServerError | HarnessFallbackError:
    """Classify a terminal turn error without exposing its details or prompt data."""
    info = payload.get("codexErrorInfo") if isinstance(payload, dict) else None
    fallback = provider_fallback(
        info,
        context="turn/completed",
        side_effects_started=side_effects_started,
        agent_session_id=agent_session_id,
    )
    return fallback or CodexAppServerError.refused_request("turn/completed")


def managed_auth_error(*, side_effects_started: bool = False, agent_session_id: str = "") -> HarnessFallbackError:
    return HarnessFallbackError(
        "Codex managed ChatGPT authentication is unavailable; "
        f"refresh pass entry {CODEX_AUTH_PASS_ENTRY!r} with "
        "`t3 codex auth import --from ~/.codex/auth.json`.",
        kind=HarnessFallbackKind.AUTH,
        side_effects_started=side_effects_started,
        agent_session_id=agent_session_id,
    )


def auth_persist_error(*, side_effects_started: bool, agent_session_id: str) -> HarnessFallbackError:
    return HarnessFallbackError(
        "Codex refreshed its managed ChatGPT authentication but TeaTree could not persist it to "
        f"pass entry {CODEX_AUTH_PASS_ENTRY!r}; the Codex thread was retained for safe resume. "
        "Repair pass, then re-import with `t3 codex auth import --from ~/.codex/auth.json` if needed.",
        kind=HarnessFallbackKind.AUTH,
        side_effects_started=side_effects_started,
        agent_session_id=agent_session_id,
    )


def transport_error(
    context: str,
    *,
    side_effects_started: bool = False,
    agent_session_id: str = "",
) -> HarnessFallbackError:
    return HarnessFallbackError(
        f"Codex App Server transport failed during {context}.",
        kind=HarnessFallbackKind.TRANSPORT,
        side_effects_started=side_effects_started,
        agent_session_id=agent_session_id,
    )


def provider_fallback(
    info: object,
    *,
    context: str,
    side_effects_started: bool = False,
    agent_session_id: str = "",
) -> HarnessFallbackError | None:
    kind = _kind(info)
    if kind is None:
        return None
    if kind is HarnessFallbackKind.AUTH:
        return managed_auth_error(
            side_effects_started=side_effects_started,
            agent_session_id=agent_session_id,
        )
    return HarnessFallbackError(
        f"Codex provider reported a retryable {kind.value} failure during {context}.",
        kind=kind,
        side_effects_started=side_effects_started,
        agent_session_id=agent_session_id,
    )


def _codex_error_info(payload: object) -> object:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        return data.get("codexErrorInfo")
    return None


def _kind(info: object) -> HarnessFallbackKind | None:
    if isinstance(info, str):
        return _STRING_KINDS.get(info)
    if not isinstance(info, dict) or len(info) != 1:
        return None
    key, details = next(iter(info.items()))
    if key not in _TRANSPORT_KEYS or not isinstance(details, dict):
        return None
    status = details.get("httpStatusCode")
    if status in _HTTP_STATUS_KINDS:
        return _HTTP_STATUS_KINDS[status]
    if isinstance(status, int) and status // 100 == _SERVER_ERROR_STATUS_CLASS:
        return HarnessFallbackKind.PROVIDER_5XX
    return HarnessFallbackKind.TRANSPORT
