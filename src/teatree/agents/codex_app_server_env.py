"""Private process environment and command resolution for Codex App Server."""

import hashlib
import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from teatree.agents.codex_app_server_options import CodexAppServerError
from teatree.forge_credentials import ROUTED_GH_KEY_ENV
from teatree.utils.agent_spawn_marker import dispatched_agent_env
from teatree.utils.hook_registry import loop_registry_dir

_PROCESS_ENV_KEYS = frozenset(
    {
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_NAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "T3_CONFIG_DB",
        "T3_CONTROL_DB_DIR",
        "T3_LOOP_REGISTRY_DIR",
        "T3_OVERLAY_NAME",
        "T3_REPO",
        "TERM",
        "TMPDIR",
        "TZ",
    }
)


def codex_command() -> tuple[str, ...]:
    executable = shutil.which("codex")
    if executable is None:
        raise CodexAppServerError.missing_binary()
    return (executable,)


_DISPATCH_ENV_KEYS = ("GH_TOKEN", ROUTED_GH_KEY_ENV, "PYTEST_XDIST_AUTO_NUM_WORKERS", "T3_LOOP_REGISTRY_DIR")


def forge_token_fingerprint(dispatch_env: Mapping[str, str] | None) -> str:
    """A short, non-reversible handle on the routed forge token, never logged; empty without one."""
    token = (dispatch_env or {}).get("GH_TOKEN", "")
    return hashlib.sha256(token.encode()).hexdigest()[:12] if token else ""


def codex_process_env(
    code_home: Path, dispatch_env: Mapping[str, str] | None = None, *, ambient: Mapping[str, str] = os.environ
) -> dict[str, str]:
    """Build a private-home environment with controlled factory runtime paths.

    Of *dispatch_env* only the routed forge token, its pass key and the pytest worker budget and pool cross.
    """
    retained = {key: value for key, value in ambient.items() if key in _PROCESS_ENV_KEYS}
    retained.update({key: value for key, value in (dispatch_env or {}).items() if key in _DISPATCH_ENV_KEYS})
    retained["T3_LOOP_REGISTRY_DIR"] = retained.get("T3_LOOP_REGISTRY_DIR") or str(loop_registry_dir())
    if author_name := retained.get("GIT_AUTHOR_NAME"):
        retained.setdefault("GIT_COMMITTER_NAME", author_name)
    if author_email := retained.get("GIT_AUTHOR_EMAIL"):
        retained.setdefault("GIT_COMMITTER_EMAIL", author_email)
    retained["GIT_CONFIG_GLOBAL"] = str(code_home / ".gitconfig")
    return {**retained, **dispatched_agent_env(), "CODEX_HOME": str(code_home), "HOME": str(code_home)}
