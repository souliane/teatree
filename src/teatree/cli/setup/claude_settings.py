"""Host-side Claude settings merge from the one committed template (#3410, #3408, #3437).

``deploy/claude-settings.template.json`` is the single source of truth for the
managed Claude Code config — model, ``permissions.defaultMode`` /
``permissions.allow``, ``autoMode.allow``, ``enabledPlugins`` (the ``t3@souliane``
skills plugin plus ``pyright-lsp@claude-plugins-official`` for live type
diagnostics), and the managed ``env`` (tool-use concurrency plus ``TMPDIR`` /
``PYTEST_DEBUG_TEMPROOT`` routing agent and pytest scratch to DISK, off the box's
small RAM-backed ``/tmp`` tmpfs). The
worker containers seed it into ``~/.claude/settings.json`` in
``deploy/entrypoint.sh`` (``seed_claude_settings``); this module gives the HOST
the identical merge so the interactive box and the containers can never drift.

Three ``TEATREE_CLAUDE_*`` env vars override the box-specific knobs (model,
permission mode, tool-use concurrency). :func:`resolve_managed_template` is the ONE
resolver that applies them, defined once in :data:`TEATREE_CLAUDE_OVERRIDES`: the
entrypoint seed invokes this module as a script to render the resolved template,
and :func:`managed_key_drift` / :func:`write_host_claude_settings` apply the same
resolver — so the container seed and the host drift check always agree on the
effective managed config instead of the host judging against the raw template.

Allow-list arrays (``permissions.allow``, ``autoMode.allow``) carry SET-UNION /
superset semantics on the host: :func:`write_host_claude_settings` unions the
template's managed grants with the operator's own additions (never dropping an
operator-added entry), and :func:`managed_key_drift` flags an allow-list only when
a template entry is MISSING from the host — an operator's extra grants are their
own and never count as drift. :func:`deep_merge` stays the generic
``jq '.[0] * .[1]'`` primitive (arrays replaced wholesale); the union lives in
:func:`merge_host_settings`, layered on top for the managed allow-lists.
"""

import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

# A parsed JSON settings object (``~/.claude/settings.json`` / the template).
# Values are arbitrary JSON, so the leaves stay ``object``; the alias names the
# shape and keeps the module-health ratchet's dataclass/TypedDict rule satisfied.
type JsonObject = dict[str, object]

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
    ("env", "TMPDIR"),
    ("env", "PYTEST_DEBUG_TEMPROOT"),
    ("enabledPlugins", "t3@souliane"),
    ("enabledPlugins", "pyright-lsp@claude-plugins-official"),
)

# The managed allow-list paths carrying set-union / superset semantics: the
# template asserts these grants are PRESENT, but operator-added entries survive a
# managed re-write and never count as drift.
ALLOW_LIST_KEY_PATHS: tuple[tuple[str, ...], ...] = (
    ("permissions", "allow"),
    ("autoMode", "allow"),
)

# The one source of truth for the ``TEATREE_CLAUDE_*`` box-knob overrides: env var
# -> the dotted managed-key path it sets on the template. Consumed by BOTH the
# entrypoint seed (via a script invocation of this module) and the host-side drift
# check / write, so the container and host can never disagree on the knobs.
TEATREE_CLAUDE_OVERRIDES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("TEATREE_CLAUDE_MODEL", ("model",)),
    ("TEATREE_CLAUDE_PERMISSION_MODE", ("permissions", "defaultMode")),
    ("TEATREE_CLAUDE_TOOL_CONCURRENCY", ("env", "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY")),
)


def deep_merge(base: JsonObject, override: JsonObject) -> JsonObject:
    """Return ``base`` deep-merged with ``override`` (override wins), mirroring ``jq '.[0] * .[1]'``.

    Object values are merged recursively; every non-object value — scalars and
    arrays alike — is replaced wholesale by ``override``'s. Neither input is
    mutated. This is the exact semantics ``seed_claude_settings`` uses
    container-side, so a host merge and a container seed of the same template
    produce the same managed config. The host layers set-union on the managed
    allow-lists on top of this primitive in :func:`merge_host_settings`.
    """
    merged: JsonObject = dict(base)
    for key, override_value in override.items():
        base_value = merged.get(key)
        if isinstance(base_value, dict) and isinstance(override_value, dict):
            merged[key] = deep_merge(cast("JsonObject", base_value), cast("JsonObject", override_value))
        else:
            merged[key] = override_value
    return merged


def _load_json_object(path: Path) -> JsonObject:
    """Load ``path`` as a JSON object, or ``{}`` when absent/empty/not an object."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _dig(data: JsonObject, path: tuple[str, ...]) -> object | None:
    """Return the value at dotted ``path`` in ``data``, or ``None`` when any hop is absent."""
    cursor: object = data
    for key in path:
        if not isinstance(cursor, dict):
            return None
        cursor_dict = cast("JsonObject", cursor)
        if key not in cursor_dict:
            return None
        cursor = cursor_dict[key]
    return cursor


def _set_path(data: JsonObject, path: tuple[str, ...], value: object) -> None:
    """Set ``value`` at dotted ``path`` in ``data``, creating intermediate objects."""
    cursor: JsonObject = data
    for key in path[:-1]:
        branch = cursor.get(key)
        if not isinstance(branch, dict):
            branch = {}
            cursor[key] = branch
        cursor = cast("JsonObject", branch)
    cursor[path[-1]] = value


def resolve_managed_template(template: JsonObject, env: Mapping[str, str]) -> JsonObject:
    """Return ``template`` with every set ``TEATREE_CLAUDE_*`` override applied.

    The single resolver the container seed and the host drift check share: each
    override in :data:`TEATREE_CLAUDE_OVERRIDES` is applied only when its env var is
    a non-empty string (mirroring the entrypoint's original ``!= ""`` gate).
    ``template`` is not mutated.
    """
    resolved = cast("JsonObject", json.loads(json.dumps(template)))
    for env_name, path in TEATREE_CLAUDE_OVERRIDES:
        value = env.get(env_name)
        if value:
            _set_path(resolved, path, value)
    return resolved


def _union_allow_list(base: list[object], template: list[object]) -> list[object]:
    """Return the template grants followed by any operator-added ``base`` entries.

    Template entries come first (managed, canonical order); any operator-added
    entry not already present is appended, so a managed re-write preserves the
    operator's extra grants instead of clobbering them.
    """
    unioned: list[object] = list(template)
    for item in base:
        if item not in unioned:
            unioned.append(item)
    return unioned


def merge_host_settings(base: JsonObject, template: JsonObject) -> JsonObject:
    """Deep-merge ``template`` into ``base`` with set-union on the managed allow-lists.

    Everything follows :func:`deep_merge` (the template's managed keys win, the
    operator's unmanaged keys survive); the managed allow-list paths then take the
    union of the operator's list and the template's, so operator-added grants are
    never dropped by a managed re-write. Neither input is mutated.
    """
    merged = deep_merge(base, template)
    for path in ALLOW_LIST_KEY_PATHS:
        base_list = _dig(base, path)
        template_list = _dig(template, path)
        if isinstance(base_list, list) and isinstance(template_list, list):
            unioned = _union_allow_list(cast("list[object]", base_list), cast("list[object]", template_list))
            _set_path(merged, path, unioned)
    return merged


def write_host_claude_settings(
    template_path: Path, target_path: Path, env: Mapping[str, str] | None = None
) -> JsonObject:
    """Merge the resolved managed template into ``target_path`` (creating it); return the result.

    The template is first passed through :func:`resolve_managed_template` (applying
    ``TEATREE_CLAUDE_*`` overrides from ``env``, defaulting to ``os.environ``), so the
    host writes the SAME effective config the container seed does; then
    :func:`merge_host_settings` unions the managed allow-lists so operator-added
    grants survive. The parent directory is created if needed. Raises
    ``FileNotFoundError`` when the template is missing or empty (a packaging bug the
    caller should surface, not swallow).
    """
    template = _load_json_object(template_path)
    if not template:
        msg = f"claude-settings template missing or empty: {template_path}"
        raise FileNotFoundError(msg)
    resolved = resolve_managed_template(template, os.environ if env is None else env)
    merged = merge_host_settings(_load_json_object(target_path), resolved)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return merged


def managed_key_drift(template_path: Path, target_path: Path, env: Mapping[str, str] | None = None) -> list[str]:
    """Return the dotted managed keys where ``target`` disagrees with the ``template``.

    The template is resolved through :func:`resolve_managed_template` first
    (``TEATREE_CLAUDE_*`` overrides from ``env``, defaulting to ``os.environ``), so an
    overridden knob is judged against its effective value, not the raw template. A
    scalar key drifts on any value mismatch; a managed allow-list drifts only when a
    template grant is MISSING from the target (operator-added grants are theirs and
    never drift). An absent target file drifts every managed key. Read-only — never
    writes either file.
    """
    template = resolve_managed_template(_load_json_object(template_path), os.environ if env is None else env)
    target = _load_json_object(target_path)
    drifted: list[str] = []
    for path in MANAGED_KEY_PATHS:
        template_value = _dig(template, path)
        if template_value is None:
            continue
        if path in ALLOW_LIST_KEY_PATHS and isinstance(template_value, list):
            target_value = _dig(target, path)
            host_items = target_value if isinstance(target_value, list) else []
            if any(item not in host_items for item in template_value):
                drifted.append(".".join(path))
        elif _dig(target, path) != template_value:
            drifted.append(".".join(path))
    return drifted


_EXPECTED_ARGC = 2  # program name + template path


def _main(argv: Sequence[str]) -> int:
    """Render the env-resolved managed template to stdout (the entrypoint seed path).

    ``argv`` is ``[prog, template_path]``. Prints the ``TEATREE_CLAUDE_*``-resolved
    template as JSON so ``deploy/entrypoint.sh`` seeds the container with the exact
    config the host drift check asserts. Returns a POSIX exit code.
    """
    if len(argv) != _EXPECTED_ARGC:
        sys.stderr.write("usage: claude_settings.py <template-path>\n")
        return 2
    template = _load_json_object(Path(argv[1]))
    if not template:
        sys.stderr.write(f"claude-settings template missing or empty: {argv[1]}\n")
        return 1
    resolved = resolve_managed_template(template, os.environ)
    sys.stdout.write(json.dumps(resolved, indent=2) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover — entrypoint seed script invocation
    raise SystemExit(_main(sys.argv))


__all__ = [
    "ALLOW_LIST_KEY_PATHS",
    "MANAGED_KEY_PATHS",
    "TEATREE_CLAUDE_OVERRIDES",
    "deep_merge",
    "managed_key_drift",
    "merge_host_settings",
    "resolve_managed_template",
    "write_host_claude_settings",
]
