"""Real checkout ownership fixture for synthetic loop ticket tests."""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import teatree
from teatree.core.overlay_loader import get_all_overlays
from teatree.utils.git import remote_slug
from teatree.utils.git_run import git_env_hermetic
from tests._git_repo import make_git_repo, run_git


@contextmanager
def installed_checkout_owner(*, claimed: bool, registered_overlays: int = 1) -> Generator[str]:
    with TemporaryDirectory() as directory:
        checkout = make_git_repo(Path(directory))
        run_git(checkout, "remote", "add", "origin", "https://github.com/example/factory.git")
        package_file = checkout / "src/teatree/__init__.py"
        package_file.parent.mkdir(parents=True)
        package_file.touch()
        overlay = get_all_overlays()["t3-teatree"]
        repos = ["example/factory"] if claimed else ["example/other"]
        overlays = {"t3-teatree": overlay}
        if registered_overlays == 2:
            overlays["other"] = SimpleNamespace(get_workspace_repos=lambda: ["example/unrelated"])
        with (
            git_env_hermetic(),
            patch.object(teatree, "__file__", str(package_file)),
            patch("teatree.paths.teatree_source_root", return_value=checkout),
            patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays),
            patch.object(overlay.config, "workspace_repos", repos),
        ):
            assert remote_slug(repo=str(checkout)) == "example/factory"
            yield "t3-teatree" if claimed else ""


@contextmanager
def installed_package_without_git(*, registered_overlays: int) -> Generator[str]:
    with TemporaryDirectory() as directory:
        package_root = Path(directory)
        package_file = package_root / "src/teatree/__init__.py"
        package_file.parent.mkdir(parents=True)
        package_file.touch()
        overlays = {"t3-teatree": get_all_overlays()["t3-teatree"]} if registered_overlays else {}
        if registered_overlays == 2:
            overlays["other"] = SimpleNamespace(get_workspace_repos=lambda: ["example/unrelated"])
        with (
            git_env_hermetic(),
            patch.object(teatree, "__file__", str(package_file)),
            patch("teatree.paths.teatree_source_root", return_value=package_root),
            patch("teatree.core.overlay_loader.get_all_overlays", return_value=overlays),
        ):
            yield "t3-teatree" if registered_overlays == 1 else ""
