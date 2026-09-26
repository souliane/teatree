# test-path: cross-cutting — drives deploy/t3 (no src mirror).
"""The session id must cross into the container, or slot 1 of the precedence is dead (#4479).

``deploy/t3`` runs the CLI inside the worker container, and a `docker exec` starts with
the DAEMON's environment — so a session id the harness exports on the host is simply
absent inside, and every resolver reading it comes back empty. That is what made the
loop-registry fallback the only answer any containerized call ever got: measured, `t3
<overlay> handover whoami` reported the tick owner while the calling session's own
``CLAUDE_CODE_SESSION_ID`` was set on the host the whole time.

The names are read from :data:`~teatree.core.session_identity.SESSION_ID_ENV_VARS`
rather than repeated here, so a future rename cannot leave the wrapper forwarding a name
nothing reads — the drift this file exists to catch would otherwise be invisible.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from teatree.core.session_identity import SESSION_ID_ENV_VARS

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash (present in the deploy image and CI)")

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"
_BASH = shutil.which("bash") or "bash"


def _slice(start: str, end: str) -> str:
    """The wrapper's own source between two markers — never a re-implementation."""
    text = WRAPPER.read_text(encoding="utf-8")
    begin = text.index(start)
    return text[begin : text.index(end, begin) + len(end)]


def _forwarded_env_args(**environ: str) -> list[str]:
    """The `--env` arguments the wrapper's OWN assembly produces for *environ*."""
    program = "\n".join(
        [
            "set -euo pipefail",
            _slice("FORWARD_ENV_NAMES=(", "\n)\n"),
            _slice("ENV_ARGS=()", "\ndone\n"),
            'printf "%s\\n" "${ENV_ARGS[@]:-}"',
        ]
    )
    done = subprocess.run(
        [_BASH, "-c", program], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", **environ}, check=True
    )
    return [line for line in done.stdout.split("\n") if line]


@pytest.mark.parametrize("name", SESSION_ID_ENV_VARS)
def test_a_set_session_id_crosses_the_boundary(name: str) -> None:
    assert _forwarded_env_args(**{name: "sess-abc"}) == ["--env", name]


@pytest.mark.parametrize("name", SESSION_ID_ENV_VARS)
def test_an_unset_session_id_forwards_nothing(name: str) -> None:
    """Forwarding an empty name would hand the container a pin that does not parse."""
    assert _forwarded_env_args() == []


def test_the_id_crosses_as_a_bare_name_never_an_argv_value() -> None:
    """A session id in an argv is world-readable, and it identifies the operator's session."""
    assert not any("=" in arg for arg in _forwarded_env_args(CLAUDE_CODE_SESSION_ID="sess-abc"))
