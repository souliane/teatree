"""The Codex plugin id exactly as the shipped Codex manifests declare it."""

import json
from pathlib import Path

_ROOT = Path(__file__).parents[2]
_MARKETPLACE = json.loads((_ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
_PLUGIN = json.loads((_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
CODEX_PLUGIN_ID: str = f"{_PLUGIN['name']}@{_MARKETPLACE['name']}"
