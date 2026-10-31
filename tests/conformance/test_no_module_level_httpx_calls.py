"""Forge traffic goes through the pooled sessions: no module under src/teatree may call a module-level httpx verb."""

import ast
from pathlib import Path

import teatree

_VERBS = frozenset({"get", "post", "put", "delete", "patch", "request", "stream", "head", "options"})
_ROOT = Path(teatree.__file__).parent

# Existing non-forge integrations. The list only shrinks: a new module-level call anywhere else fails.
_NON_FORGE_INTEGRATIONS = frozenset(
    {
        "backends/slack/client.py",
        "backends/slack/http.py",
        "backends/slack/reactions.py",
        "backends/slack/socket_mode.py",
        "browser/session.py",
        "cli/slack/app_resolve.py",
        "cli/slack/manifest.py",
        "cli/slack/user_token_setup.py",
        "core/connector_probes.py",
        "core/worktree/readiness.py",
        "llm/rate_limits.py",
    }
)


def _module_level_httpx_calls(path: Path) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _VERBS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "httpx"
    ]


def test_no_new_module_calls_a_module_level_httpx_verb() -> None:
    offenders = {
        name: lines
        for path in sorted(_ROOT.rglob("*.py"))
        if (name := path.relative_to(_ROOT).as_posix()) not in _NON_FORGE_INTEGRATIONS
        and (lines := _module_level_httpx_calls(path))
    }

    assert offenders == {}, f"use the pooled forge session (teatree.backends.forge_pool) instead: {offenders}"


def test_the_allowlist_names_only_modules_that_still_call_one() -> None:
    assert [name for name in sorted(_NON_FORGE_INTEGRATIONS) if not _module_level_httpx_calls(_ROOT / name)] == []
