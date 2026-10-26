"""Real checkout ownership fixture for synthetic loop ticket tests."""

import os
import shutil
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import teatree
from teatree.core.overlay_loader import get_all_overlays
from teatree.utils.git import remote_slug
from teatree.utils.git_run import git_env_without_overrides


@contextmanager
def installed_checkout_owner(*, claimed: bool) -> Generator[str]:
    with TemporaryDirectory() as directory:
        checkout = Path(directory)
        environment = git_env_without_overrides()
        git = shutil.which("git")
        assert git is not None
        subprocess.run([git, "init", "-b", "main", str(checkout)], check=True, capture_output=True, env=environment)
        subprocess.run(
            [git, "-C", str(checkout), "remote", "add", "origin", "https://github.com/example/factory.git"],
            check=True,
            capture_output=True,
            env=environment,
        )
        package_file = checkout / "src/teatree/__init__.py"
        package_file.parent.mkdir(parents=True)
        package_file.touch()
        overlay = get_all_overlays()["t3-teatree"]
        repos = ["example/factory"] if claimed else ["example/other"]
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(teatree, "__file__", str(package_file)),
            patch.object(overlay.config, "workspace_repos", repos),
        ):
            assert remote_slug(repo=str(checkout)) == "example/factory"
            yield "t3-teatree" if claimed else ""
