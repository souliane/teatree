"""Private process environment and command resolution for Codex App Server."""

import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from teatree.agents.codex_app_server_options import CodexAppServerError

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


def codex_process_env(code_home: Path, *, ambient: Mapping[str, str] = os.environ) -> dict[str, str]:
    """Build a private-home environment with controlled factory runtime paths."""
    retained = {key: value for key, value in ambient.items() if key in _PROCESS_ENV_KEYS}
    if author_name := retained.get("GIT_AUTHOR_NAME"):
        retained.setdefault("GIT_COMMITTER_NAME", author_name)
    if author_email := retained.get("GIT_AUTHOR_EMAIL"):
        retained.setdefault("GIT_COMMITTER_EMAIL", author_email)
    retained.update(
        {
            "GIT_CONFIG_GLOBAL": str(code_home / ".gitconfig"),
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
        }
    )
    return {**retained, "CODEX_HOME": str(code_home), "HOME": str(code_home)}
