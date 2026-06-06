"""``t3 eval all --docker`` — run the eval gate inside the exact CI image.

Reuses ``dev/Dockerfile.test`` (the image the CI test job builds) so a local
``--docker`` run reproduces CI's environment exactly. Local host-run is the
default; ``--docker`` is the opt-in parity path. No PyPI — the image installs
the working tree via the mounted repo and ``uv``.
"""

import shutil
from pathlib import Path

from teatree.utils.run import run_allowed_to_fail, run_streamed

DOCKER_IMAGE = "teatree-test"
_DOCKERFILE = "dev/Dockerfile.test"


class DockerUnavailableError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("docker is not on PATH; install Docker or run `t3 eval all` on the host (the default).")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _image_present() -> bool:
    return run_allowed_to_fail(["docker", "image", "inspect", DOCKER_IMAGE], expected_codes=None).returncode == 0


def _build_image(root: Path) -> int:
    return run_streamed(["docker", "build", "-q", "-t", DOCKER_IMAGE, "-f", _DOCKERFILE, "."], cwd=root, check=False)


def _run_in_image(root: Path, eval_args: list[str]) -> int:
    return run_streamed(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            "UV_PROJECT_ENVIRONMENT=/tmp/.venv",
            "-e",
            "HOME=/tmp",
            "-v",
            f"{root}:/app:ro",
            DOCKER_IMAGE,
            "uv",
            "run",
            "t3",
            "eval",
            *eval_args,
        ],
        cwd=root,
        check=False,
    )


def run_eval_in_docker(eval_args: list[str]) -> int:
    """Build (if needed) and run the eval gate inside the CI image; return its exit code."""
    if shutil.which("docker") is None:
        raise DockerUnavailableError
    root = _repo_root()
    if not _image_present():
        build_code = _build_image(root)
        if build_code != 0:
            return build_code
    return _run_in_image(root, eval_args)
