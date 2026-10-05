"""APM dependency install + apm-hook stripping for ``t3 setup``."""

import json
from pathlib import Path


def strip_apm_hooks(settings_path: Path) -> int:
    """Remove hook entries injected by APM (_apm_source key) from settings.json."""
    if not settings_path.is_file():
        return 0

    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0

    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        return 0

    removed = 0
    keys_to_check = list(hooks.keys())
    for key in keys_to_check:
        entries = hooks[key]
        if isinstance(entries, list):
            original_len = len(entries)
            hooks[key] = [e for e in entries if not (isinstance(e, dict) and "_apm_source" in e)]
            removed += original_len - len(hooks[key])
            if not hooks[key]:
                del hooks[key]

    if not hooks and "hooks" in data:
        del data["hooks"]

    if removed > 0:
        settings_path.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")

    return removed
