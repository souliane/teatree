# test-path: cross-cutting
"""`deploy/deploy.sh` must be directly executable (#4822).

A non-executable deploy.sh makes `./deploy/deploy.sh` fail with "Permission
denied" — every invocation is then forced through `bash deploy/deploy.sh`, an
easy-to-miss requirement that silently hides real invocation errors behind an
unrelated shell error.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "deploy" / "deploy.sh"
_GIT = shutil.which("git") or "/usr/bin/git"


class TestDeploySHIsExecutable:
    def test_the_filesystem_bit_grants_execute(self) -> None:
        assert os.access(_SCRIPT, os.X_OK), f"{_SCRIPT} is not executable on disk"

    def test_the_git_tracked_mode_is_100755(self) -> None:
        # The filesystem bit alone doesn't survive a fresh clone unless git
        # itself tracks the executable mode — assert the tracked mode too.
        result = subprocess.run(
            [_GIT, "-C", str(_ROOT), "ls-files", "-s", "deploy/deploy.sh"],
            check=True,
            capture_output=True,
            text=True,
        )
        mode = result.stdout.split()[0]
        assert mode == "100755", f"deploy/deploy.sh is tracked as {mode}, expected 100755 (git ls-files -s)"

    def test_every_owner_group_other_execute_bit_is_set(self) -> None:
        mode = stat.S_IMODE(_SCRIPT.stat().st_mode)
        assert mode & 0o111 == 0o111, f"deploy/deploy.sh mode is {oct(mode)}, missing an execute bit"
