"""Host-side Claude settings merge from the one committed template (#3410, #3408).

``deploy/claude-settings.template.json`` is the single source of truth for the
managed Claude Code config — model, ``permissions.defaultMode`` /
``permissions.allow``, ``autoMode.allow``, and the tool-use-concurrency env. The
worker containers seed it into ``~/.claude/settings.json`` in
``deploy/entrypoint.sh`` (``seed_claude_settings``, a ``jq '.[0] * .[1]'`` deep
merge). This module gives the HOST the identical merge so the interactive box
and the containers can never drift: :func:`write_host_claude_settings` deep-merges
the same template into the host's ``~/.claude/settings.json``, preserving
user-specific keys (``statusLine``, any extra host rules) while asserting the
managed keys.

:func:`deep_merge` mirrors ``jq``'s ``*`` operator exactly — objects merge
recursively, every non-object value (scalars AND arrays) is replaced by the
override — so the host and container settings resolve byte-for-byte the same
managed config. :func:`managed_key_drift` powers the ``t3 doctor`` gate that
verifies the host agrees with the template.
"""

import json
from pathlib import Path
from typing import cast

# The managed leaf keys the template owns, addressed as dotted paths into the
# settings object. Everything else in ``~/.claude/settings.json`` (statusLine,
# user rules) is the operator's and is never asserted. Drift on any of these is
# what the doctor gate reports.
MANAGED_KEY_PATHS: tuple[tuple[str, ...], ...] = (
    ("model",),
    ("permissions", "defaultMode"),
    ("permissions", "allow"),
    ("autoMode", "allow"),
    ("env", "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY"),
)


def deep_merge(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    """Return ``base`` deep-merged with ``override`` (override wins), mirroring ``jq '.[0] * .[1]'``.

    Object values are merged recursively; every non-object value — scalars and
    arrays alike — is replaced wholesale by ``override``'s. Neither input is
    mutated. This is the exact semantics ``seed_claude_settings`` uses
    container-side, so a host merge and a container seed of the same template
    produce the same managed config.
    """
    merged: dict[str, object] = dict(base)
    for key, override_value in override.items():
        base_value = merged.get(key)
        if isinstance(base_value, dict) and isinstance(override_value, dict):
            merged[key] = deep_merge(cast("dict[str, object]", base_value), cast("dict[str, object]", override_value))
        else:
            merged[key] = override_value
    return merged


def _load_json_object(path: Path) -> dict[str, object]:
    """Load ``path`` as a JSON object, or ``{}`` when absent/empty/not an object."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def write_host_claude_settings(template_path: Path, target_path: Path) -> dict[str, object]:
    """Deep-merge ``template_path`` into ``target_path`` (creating it if absent); return the result.

    The existing target is the LEFT operand and the template is the RIGHT, so the
    template's managed keys win while the operator's unmanaged keys survive —
    identical to the container seed. The parent directory is created if needed.
    Raises ``FileNotFoundError`` when the template is missing (a packaging bug the
    caller should surface, not swallow).
    """
    template = _load_json_object(template_path)
    if not template:
        msg = f"claude-settings template missing or empty: {template_path}"
        raise FileNotFoundError(msg)
    merged = deep_merge(_load_json_object(target_path), template)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return merged


def _dig(data: dict[str, object], path: tuple[str, ...]) -> object | None:
    """Return the value at dotted ``path`` in ``data``, or ``None`` when any hop is absent."""
    cursor: object = data
    for key in path:
        if not isinstance(cursor, dict):
            return None
        cursor_dict = cast("dict[str, object]", cursor)
        if key not in cursor_dict:
            return None
        cursor = cursor_dict[key]
    return cursor


def managed_key_drift(template_path: Path, target_path: Path) -> list[str]:
    """Return the dotted managed keys where ``target`` disagrees with the ``template``.

    A key is drifted when the target's value differs from the template's (a key
    the template does not carry is not managed and never drifts). An absent
    target file drifts every managed key. Read-only — never writes either file.
    """
    template = _load_json_object(template_path)
    target = _load_json_object(target_path)
    drifted: list[str] = []
    for path in MANAGED_KEY_PATHS:
        template_value = _dig(template, path)
        if template_value is None:
            continue
        if _dig(target, path) != template_value:
            drifted.append(".".join(path))
    return drifted


__all__ = [
    "MANAGED_KEY_PATHS",
    "deep_merge",
    "managed_key_drift",
    "write_host_claude_settings",
]
