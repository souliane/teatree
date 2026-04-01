import json
from copy import deepcopy
from pathlib import Path

from django.conf import settings

_DEFAULT_STATUSLINE_STATE_DIR = Path("/tmp/claude-statusline")  # noqa: S108
_DEFAULT_AGENT_HANDOVER = [
    {
        "runtime": "claude-code",
        "telemetry": {
            "provider": "claude-statusline",
            "switch_away_at_percent": 95,
            "switch_back_at_percent": 80,
        },
    },
]


def get_claude_statusline_state_dir(*, state_dir: Path | str | None = None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    configured = getattr(settings, "TEATREE_CLAUDE_STATUSLINE_STATE_DIR", _DEFAULT_STATUSLINE_STATE_DIR)
    return Path(configured)


def get_agent_handover_config() -> list[dict[str, object]]:
    configured = getattr(settings, "TEATREE_AGENT_HANDOVER", _DEFAULT_AGENT_HANDOVER)
    if not isinstance(configured, list):
        return deepcopy(_DEFAULT_AGENT_HANDOVER)

    normalized: list[dict[str, object]] = []
    for item in configured:
        if not isinstance(item, dict):
            continue
        runtime = item.get("runtime")
        if not isinstance(runtime, str) or not runtime:
            continue
        normalized_item: dict[str, object] = {"runtime": runtime}
        telemetry = item.get("telemetry")
        if isinstance(telemetry, dict):
            normalized_item["telemetry"] = dict(telemetry)
        normalized.append(normalized_item)
    return normalized or deepcopy(_DEFAULT_AGENT_HANDOVER)


def _get_runtime_policy(runtime: str) -> dict[str, object]:
    for item in get_agent_handover_config():
        if item.get("runtime") == runtime:
            return item
    return {}


def _get_next_runtime(runtime: str) -> str:
    config = get_agent_handover_config()
    for index, item in enumerate(config):
        if item.get("runtime") == runtime:
            next_index = index + 1
            return str(config[next_index]["runtime"]) if next_index < len(config) else ""
    return ""


def _get_preferred_runtime() -> str:
    config = get_agent_handover_config()
    return str(config[0]["runtime"]) if config else ""


def _get_switch_threshold(runtime: str, field_name: str) -> int | None:
    telemetry = _get_runtime_policy(runtime).get("telemetry")
    if not isinstance(telemetry, dict):
        return None
    value = telemetry.get(field_name)  # type: ignore[arg-type]
    if not isinstance(value, int | float):
        return None
    return max(0, min(100, int(value)))


def get_claude_telemetry_path(session_id: str = "", *, state_dir: Path | str | None = None) -> Path:
    root = get_claude_statusline_state_dir(state_dir=state_dir)
    if session_id:
        return root / f"{session_id}.telemetry.json"
    return root / "latest-telemetry.json"


def load_claude_telemetry(session_id: str = "", *, state_dir: Path | str | None = None) -> dict[str, object]:
    path = get_claude_telemetry_path(session_id=session_id, state_dir=state_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def should_suggest_handover(telemetry: dict[str, object] | None, *, runtime: str) -> bool:
    if not telemetry:
        return False
    threshold = _get_switch_threshold(runtime, "switch_away_at_percent")
    if threshold is None:
        return False
    used = telemetry.get("five_hour_used_percentage")
    if not isinstance(used, int | float):
        return False
    return int(used) >= threshold


def get_recommended_runtime(current_runtime: str, telemetry: dict[str, object] | None) -> str:
    if should_suggest_handover(telemetry, runtime=current_runtime):
        return _get_next_runtime(current_runtime)

    preferred_runtime = _get_preferred_runtime()
    if not preferred_runtime or current_runtime == preferred_runtime or not telemetry:
        return ""

    recovery_threshold = _get_switch_threshold(preferred_runtime, "switch_back_at_percent")
    used = telemetry.get("five_hour_used_percentage")
    if recovery_threshold is None or not isinstance(used, int | float):
        return ""
    return preferred_runtime if int(used) <= recovery_threshold else ""


def build_claude_handover_status(
    *,
    current_runtime: str = "",
    session_id: str = "",
    state_dir: Path | str | None = None,
) -> dict[str, object]:
    telemetry = load_claude_telemetry(session_id=session_id, state_dir=state_dir)
    preferred_runtime = _get_preferred_runtime()
    active_runtime = current_runtime or preferred_runtime
    recommended_runtime = get_recommended_runtime(active_runtime, telemetry)
    return {
        "session_id": str(telemetry.get("session_id", session_id)),
        "telemetry_available": bool(telemetry),
        "current_runtime": active_runtime,
        "preferred_runtime": preferred_runtime,
        "recommended_runtime": recommended_runtime,
        "agent_handover": get_agent_handover_config(),
        "five_hour_used_percentage": telemetry.get("five_hour_used_percentage"),
        "five_hour_resets_at": str(telemetry.get("five_hour_resets_at", "")),
        "context_window_used_percentage": telemetry.get("context_window_used_percentage"),
        "should_handover": bool(recommended_runtime),
    }
