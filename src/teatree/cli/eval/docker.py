"""Run the eval gate inside the exact CI image (``dev/Dockerfile.test``).

Reuses ``dev/Dockerfile.test`` (the image the CI test job builds) so a containerized
run reproduces CI's environment exactly. The fresh-run AI lane (``t3 eval run
--backend sdk``) and ``t3 eval benchmark`` default to running IN the container —
the reproducible gate must never accidentally run a model on the host. The free /
deterministic lanes stay host-default; ``--local`` is the explicit host escape
hatch for durable-history gates or quick checks. No PyPI — the image installs
the working tree via the mounted repo and ``uv``. The metered lane authenticates
EXCLUSIVELY via the metered ``ANTHROPIC_API_KEY``, never the subscription token.

The fresh-run AI lane drives the in-process ``claude-agent-sdk`` (NOT ``claude -p``)
inside the container, authenticated by the metered ``ANTHROPIC_API_KEY`` — the
metered eval lane never rides the subscription OAuth token (a full run would
throttle it). :func:`_auth_passthrough_flags` forwards the host's key via
``docker run -e VARNAME`` — the value travels through the container env, never
argv, so it never lands in the process list or logs. ``HOME=/tmp`` keeps the
virgin isolation (issue #1805).

To break the re-route loop, :func:`_run_in_image` sets ``T3_EVAL_IN_CONTAINER=1``
on the container env. The fresh-run/benchmark command runs DIRECTLY in-process when
that marker is present (or ``--local`` was passed); otherwise it routes back
through :func:`run_eval_in_docker`, which builds the image if needed and re-invokes
``t3 eval <args>`` (the same args) inside the container.
"""

import os
import shutil
from pathlib import Path

from teatree.eval.backends import SDK_BACKEND
from teatree.llm.credentials import AnthropicApiKeyCredential
from teatree.utils.run import run_allowed_to_fail, run_streamed

DOCKER_IMAGE = "teatree-test"
_DOCKERFILE = "dev/Dockerfile.test"
#: The metered eval lane authenticates EXCLUSIVELY via ``ANTHROPIC_API_KEY``; the
#: subscription OAuth token is deliberately NOT forwarded into the container, so
#: the in-container SDK can never bill the subscription (a full run throttles it).
_AUTH_ENV_VARS = (AnthropicApiKeyCredential().spec.env_var,)
#: Env marker set on the container so the in-container ``t3 eval`` re-invocation
#: runs the metered/benchmark command in-process instead of re-routing to docker.
IN_CONTAINER_ENV_VAR = "T3_EVAL_IN_CONTAINER"
#: Fixed container mount point for the WRITABLE artifacts directory. The repo is
#: mounted ``:ro`` (a metered run must never mutate the working tree), so a run
#: that emits an artifact (the per-trial transcript report) writes it here, on a
#: separate bind-mount, where it lands back on the host for upload.
ARTIFACTS_MOUNT = "/artifacts"


def _auth_passthrough_flags() -> list[str]:
    return [flag for var in _AUTH_ENV_VARS if os.environ.get(var) for flag in ("-e", var)]


def _requests_sdk_lane(eval_args: list[str]) -> bool:
    return SDK_BACKEND in eval_args


class DockerUnavailableError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("docker is not on PATH; install Docker or run `t3 eval` on the host (the default).")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _image_present() -> bool:
    return run_allowed_to_fail(["docker", "image", "inspect", DOCKER_IMAGE], expected_codes=None).returncode == 0


def _build_image(root: Path) -> int:
    # No ``-q``: a quiet build emits NOTHING until it finishes, so a slow/hung
    # image build is indistinguishable from a wedged runner. Streaming the build
    # log makes the build's progress (and any stall) visible in the CI log.
    return run_streamed(["docker", "build", "-t", DOCKER_IMAGE, "-f", _DOCKERFILE, "."], cwd=root, check=False)


def _artifacts_mount_flags(artifacts_dir: Path | None) -> list[str]:
    """Bind-mount *artifacts_dir* WRITABLE at :data:`ARTIFACTS_MOUNT`, or nothing.

    The repo mount is ``:ro``; a run that emits an artifact writes it into this
    separate writable bind-mount, so the file lands on the host for upload. No
    flag is added when the run emits no artifact.
    """
    if artifacts_dir is None:
        return []
    return ["-v", f"{artifacts_dir}:{ARTIFACTS_MOUNT}"]


def _run_in_image(root: Path, eval_args: list[str], *, artifacts_dir: Path | None = None) -> int:
    return run_streamed(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            "UV_PROJECT_ENVIRONMENT=/tmp/.venv",
            "-e",
            "HOME=/tmp",
            # Unbuffered stdio so the in-container ``t3 eval`` per-scenario progress
            # lines flush to the runner's log in real time instead of being held in
            # a pipe buffer until the process exits (a buffered hang shows nothing).
            "-e",
            "PYTHONUNBUFFERED=1",
            "-e",
            f"{IN_CONTAINER_ENV_VAR}=1",
            *_auth_passthrough_flags(),
            "-v",
            f"{root}:/app:ro",
            *_artifacts_mount_flags(artifacts_dir),
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


def run_eval_in_docker(eval_args: list[str], *, artifacts_dir: Path | None = None) -> int:
    """Build (if needed) and run the eval gate inside the CI image; return its exit code.

    For the metered ``sdk`` lane, resolve ``ANTHROPIC_API_KEY`` first via the
    canonical credential layer (:class:`~teatree.llm.credentials.AnthropicApiKeyCredential`;
    env wins, else exported from the ``pass`` store; a missing key fails loud with
    :class:`~teatree.llm.credentials.CredentialError`) so
    :func:`_auth_passthrough_flags` forwards it with ``-e`` and the in-process
    Agent SDK's ``claude`` child authenticates in-container on the metered API —
    the metered run just works without a manual ``export`` and never bills the
    subscription. The free / transcript lanes never authenticate ``claude``, so the
    secret store is not touched for them.

    ``artifacts_dir`` (when set) is bind-mounted WRITABLE at
    :data:`ARTIFACTS_MOUNT` so an in-container run that emits an artifact (the
    per-trial transcript report) writes it there and the file lands back on the
    host — the repo mount itself is ``:ro``.
    """
    if shutil.which("docker") is None:
        raise DockerUnavailableError
    if _requests_sdk_lane(eval_args):
        AnthropicApiKeyCredential().export()
    root = _repo_root()
    if not _image_present():
        build_code = _build_image(root)
        if build_code != 0:
            return build_code
    return _run_in_image(root, eval_args, artifacts_dir=artifacts_dir)
