"""Shared real-git helpers for the teatree.core cleanup test package.

Lifted verbatim from the former monolithic
``tests/teatree_core/test_cleanup.py`` (souliane/teatree#443). No behavior
change: the same ``GIT_*``-stripped environment and ``git -C`` runner the
real-git integration and #835 recovery tests share, relocated so each focused
test module can import them.
"""

import os
import shutil
import subprocess
from pathlib import Path

_GIT = shutil.which("git") or "/usr/bin/git"
_RM = shutil.which("rm") or "/bin/rm"


def _clean_env() -> dict[str, str]:
    """Env with all ``GIT_*`` stripped (AGENTS.md § Test-Writing Doctrine, #288).

    The suite can run from the inline pre-commit ``pytest`` hook, where the
    outer ``git commit`` exports ``GIT_DIR``/``GIT_INDEX_FILE``/``GIT_WORK_TREE``.
    Inherited, they hijack the tmp-repo ``git`` calls so a test that passes
    standalone corrupts the real repo under ``git commit``.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _run_git(*args: str, cwd: Path) -> None:
    subprocess.run([_GIT, "-C", str(cwd), *args], check=True, capture_output=True, env=_clean_env())
