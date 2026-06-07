"""Virgin-environment isolation for the ``claude -p`` eval subprocesses.

The core harness shells out to ``claude`` to both produce a run
(:class:`~teatree.eval.runner.ClaudePRunner`) and grade one
(:class:`~teatree.eval.judge.ClaudeJudge`). Without isolation the child
auto-discovers the developer's personal context — ``~/.claude/CLAUDE.md``,
auto-memory, and the project ``CLAUDE.md`` reachable from the parent cwd — and a
scenario can pass because the agent *remembers* a rule rather than because a
teatree gate enforces it. That biases every real eval result.

:func:`isolated_claude_env` yields the ``(env, cwd)`` pair both invoke paths pass
to :func:`~teatree.utils.run.run_allowed_to_fail`: a copy of the parent
environment with ``HOME`` (and the related config-dir vars) redirected at a
freshly created, ``.claude``-free temp directory, and that same directory as a
neutral cwd. Credential and PATH vars survive untouched, so the SDK backend's
auth (``CLAUDE_CODE_OAUTH_TOKEN`` or ``ANTHROPIC_API_KEY``) keeps working; only
the personal-context discovery roots move. The command also carries ``--bare``
(set by each caller), which disables
auto-memory and CLAUDE.md auto-discovery at the source — the env/cwd redirect is
the belt to that flag's suspenders, and keeps the run virgin even if a caller is
invoked directly without ``--bare``.
"""

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_HOME_ANCHORED_VARS = ("HOME", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR")


@contextmanager
def isolated_claude_env() -> Iterator[tuple[dict[str, str], str]]:
    """Yield ``(env, cwd)`` that runs ``claude`` free of the developer's context.

    ``env`` is the parent environment with the home-anchored discovery roots
    pointed at a private empty directory; ``cwd`` is that directory. The
    directory is removed when the context exits.
    """
    with tempfile.TemporaryDirectory(prefix="t3-eval-virgin-home-") as home:
        env = dict(os.environ)
        env["HOME"] = home
        env["XDG_CONFIG_HOME"] = str(Path(home) / ".config")
        env["CLAUDE_CONFIG_DIR"] = str(Path(home) / ".claude")
        yield env, home


__all__ = ["isolated_claude_env"]
