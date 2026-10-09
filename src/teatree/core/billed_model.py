from collections.abc import Mapping

_TOKEN_KEYS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")


def dominant_model(model_usage: object) -> str | None:
    """The key that billed the most tokens — Claude Code lists its auxiliary model first."""
    if not isinstance(model_usage, Mapping) or not model_usage:
        return None
    volumes = {str(model): _token_volume(entry) for model, entry in model_usage.items()}
    return max(volumes, key=volumes.__getitem__)


def _token_volume(entry: object) -> int:
    counts = [entry.get(key) for key in _TOKEN_KEYS] if isinstance(entry, Mapping) else []
    return sum(count for count in counts if isinstance(count, int) and not isinstance(count, bool))
