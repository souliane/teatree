"""``t3 eval all --docker`` — run the eval gate inside the exact CI image.

Reuses ``dev/Dockerfile.test`` (the image the CI test job builds) so a local
``--docker`` run reproduces CI's environment exactly. Local host-run is the
default; ``--docker`` is the opt-in parity path. No PyPI — the image installs
the working tree via the mounted repo and ``uv``.

The metered AI lane (``--backend sdk``) shells out to ``claude -p`` inside the
container, authenticated by ``CLAUDE_CODE_OAUTH_TOKEN`` (headless OAuth, no
login state needed) or ``ANTHROPIC_API_KEY``. :func:`_auth_passthrough_flags`
forwards whichever is set on the host via ``docker run -e VARNAME`` — the value
travels through the container env, never argv, so it never lands in the process
list or logs. ``HOME=/tmp`` keeps the virgin isolation (issue #1805).
"""

import os
import shutil
from pathlib import Path

from teatree.eval.auth import ensure_oauth_token
from teatree.eval.backends import SDK_BACKEND
from teatree.utils.run import run_allowed_to_fail, run_streamed

DOCKER_IMAGE = "teatree-test"
_DOCKERFILE = "dev/Dockerfile.test"
_AUTH_ENV_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


def _auth_passthrough_flags() -> list[str]:
    return [flag for var in _AUTH_ENV_VARS if os.environ.get(var) for flag in ("-e", var)]


def _requests_sdk_lane(eval_args: list[str]) -> bool:
    return SDK_BACKEND in eval_args


class DockerUnavailableError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("docker is not on PATH; install Docker or run `t3 eval all` on the host (the default).")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


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
            *_auth_passthrough_flags(),
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
    """Build (if needed) and run the eval gate inside the CI image; return its exit code.

    For the metered ``sdk`` lane, resolve ``CLAUDE_CODE_OAUTH_TOKEN`` first (env
    wins, else exported from the ``pass`` store) so :func:`_auth_passthrough_flags`
    forwards it with ``-e`` and ``claude -p`` authenticates in-container — local
    ``--backend sdk --docker`` just works without a manual ``export``. The free /
    subscription lanes never authenticate ``claude``, so the secret store is not
    touched for them.
    """
    if shutil.which("docker") is None:
        raise DockerUnavailableError
    if _requests_sdk_lane(eval_args):
        ensure_oauth_token()
    root = _repo_root()
    if not _image_present():
        build_code = _build_image(root)
        if build_code != 0:
            return build_code
    return _run_in_image(root, eval_args)
