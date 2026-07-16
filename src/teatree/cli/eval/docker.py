"""Run the eval gate inside the exact CI image (``dev/Dockerfile.test``).

Reuses ``dev/Dockerfile.test`` (the image the CI test job builds) so a containerized
run reproduces CI's environment exactly. The fresh-run AI lane (``t3 eval run
--backend api``) and ``t3 eval benchmark`` default to running IN the container —
the reproducible gate must never accidentally run a model on the host. The free /
deterministic lanes stay host-default; ``--local`` is the explicit host escape
hatch for durable-history gates or quick checks. No PyPI — the image installs
the working tree via the mounted repo and ``uv``. The fresh-run lane authenticates
with the credential the ``eval_credential`` knob selects — the subscription OAuth
token by DEFAULT (reversing #2707) or the metered ``ANTHROPIC_API_KEY`` under the
``metered_api_key`` knob.

The fresh-run AI lane drives the in-process ``claude-agent-sdk`` (NOT ``claude -p``)
inside the container, authenticated by the SELECTED eval credential (resolved via
:func:`~teatree.credential_config.resolve_eval_credential`).
:func:`_auth_passthrough_flags` forwards the host's credential var (plus the
``T3_EVAL_CREDENTIAL`` knob override) via ``docker run -e VARNAME`` — the value
travels through the container env, never argv, so it never lands in the process
list or logs. The default subscription lane must be right-sized (single effort
tier, smaller trial count, per-account OAuth routing) so its usage window is not
throttled mid-run; the metered lane has a per-token cost instead. ``HOME=/tmp``
keeps the virgin isolation (issue #1805).

To break the re-route loop, :func:`_run_in_image` sets ``T3_EVAL_IN_CONTAINER=1``
on the container env. The fresh-run/benchmark command runs DIRECTLY in-process when
that marker is present (or ``--local`` was passed); otherwise it routes back
through :func:`run_eval_in_docker`, which builds the image if needed and re-invokes
``t3 eval <args>`` (the same args) inside the container.
"""

import os
import shutil
from pathlib import Path

from teatree.eval.backends import FRESH_CLAUDE_BACKENDS
from teatree.utils.django_bootstrap import ensure_django
from teatree.utils.eval_container import IN_CONTAINER_ENV_VAR
from teatree.utils.run import run_allowed_to_fail, run_streamed

DOCKER_IMAGE = "teatree-test"
_DOCKERFILE = "dev/Dockerfile.test"
#: The ``eval_credential`` knob env override. Forwarded into the container (when set
#: on the host) so the in-container ``make_runner`` re-invocation resolves the SAME
#: credential KIND — CI wires it so the choice is deterministic without depending on
#: a ``ConfigSetting`` row inside the ephemeral container.
EVAL_CREDENTIAL_ENV_VAR = "T3_EVAL_CREDENTIAL"
#: The eval subcommands that are ALWAYS a fresh metered run regardless of an
#: explicit ``--backend`` flag. ``benchmark`` never passes a backend — it is an
#: sdk fresh-run by construction — so the eval-credential pre-export must fire for
#: it the same as for an explicit ``--backend api`` run.
_ALWAYS_METERED_SUBCOMMANDS = ("benchmark",)
#: Fixed container mount point for the WRITABLE artifacts directory. The repo is
#: mounted ``:ro`` (a metered run must never mutate the working tree), so a run
#: that emits an artifact (the per-trial transcript report) writes it here, on a
#: separate bind-mount, where it lands back on the host for upload.
ARTIFACTS_MOUNT = "/artifacts"


def _auth_passthrough_flags(auth_env_vars: tuple[str, ...]) -> list[str]:
    """``-e VARNAME`` flags forwarding the SELECTED eval credential + the knob override.

    Forwards each var in *auth_env_vars* (the resolved eval credential's env var —
    ``CLAUDE_CODE_OAUTH_TOKEN`` by default, ``ANTHROPIC_API_KEY`` under the metered
    knob) present on the host, plus the ``T3_EVAL_CREDENTIAL`` knob override so the
    in-container re-invocation resolves the same credential kind. The VALUE travels
    through the container env (docker's ``-e VARNAME`` reads it from the host env),
    never argv — so the secret never lands in the process list or logs.
    """
    forward = (*auth_env_vars, EVAL_CREDENTIAL_ENV_VAR)
    return [flag for var in forward if os.environ.get(var) for flag in ("-e", var)]


#: The CI checkout SHA the ``--summary-json`` artifact records as ``head_sha`` — it
#: is written in-container, so the value must be forwarded through the container
#: env (never argv), and only when GitHub Actions set it.
_HEAD_SHA_ENV_VAR = "GITHUB_SHA"


def _head_sha_passthrough_flags() -> list[str]:
    """Forward ``GITHUB_SHA`` into the container when set, so the JSON records the real SHA."""
    return ["-e", _HEAD_SHA_ENV_VAR] if os.environ.get(_HEAD_SHA_ENV_VAR) else []


def _requests_api_lane(eval_args: list[str]) -> bool:
    """Whether *eval_args* drives a metered fresh-Claude lane, so the credential pre-export must fire.

    True for an explicit ``--backend api`` OR ``--backend anthropic_api`` run (both
    RUN a Claude model and bill the Anthropic account — the CLI-free ``anthropic_api``
    lane authenticates on ``ANTHROPIC_API_KEY`` and must have it forwarded into the
    container too) AND for an always-metered subcommand (``benchmark``) that bills the
    API by construction without ever passing ``--backend``. Keying on the exact
    :data:`FRESH_CLAUDE_BACKENDS` tokens plus the metered subcommand — never a stray
    literal — means the credential resolves (and fails loud when absent) BEFORE any
    Docker build/run on every metered path.
    """
    if any(backend in eval_args for backend in FRESH_CLAUDE_BACKENDS):
        return True
    return bool(eval_args) and eval_args[0] in _ALWAYS_METERED_SUBCOMMANDS


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


def _run_in_image(
    root: Path, eval_args: list[str], *, auth_env_vars: tuple[str, ...] = (), artifacts_dir: Path | None = None
) -> int:
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
            *_auth_passthrough_flags(auth_env_vars),
            *_head_sha_passthrough_flags(),
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

    For the fresh-run ``api`` lane, resolve the SELECTED eval credential first via
    the single seam (:func:`~teatree.credential_config.resolve_eval_credential` —
    default subscription OAuth, reversing #2707; env wins, else exported from the
    ``pass`` store; a missing credential fails loud with
    :class:`~teatree.llm.credentials.CredentialError`) so
    :func:`_auth_passthrough_flags` forwards its env var with ``-e`` and the
    in-process Agent SDK's ``claude`` child authenticates in-container on the
    selected credential — the run just works without a manual ``export``. The
    ``T3_EVAL_CREDENTIAL`` knob override is forwarded alongside so the in-container
    ``make_runner`` resolves the same kind. The free / transcript lanes never
    authenticate ``claude``, so the secret store is not touched for them (empty
    ``auth_env_vars``).

    ``artifacts_dir`` (when set) is bind-mounted WRITABLE at
    :data:`ARTIFACTS_MOUNT` so an in-container run that emits an artifact (the
    per-trial transcript report) writes it there and the file lands back on the
    host — the repo mount itself is ``:ro``.
    """
    if shutil.which("docker") is None:
        raise DockerUnavailableError
    auth_env_vars: tuple[str, ...] = ()
    if _requests_api_lane(eval_args):
        # This is the single chokepoint every caller (``eval run``, ``eval
        # benchmark``, the bare full-suite lane) routes through before Docker, so
        # ``ensure_django()`` must run HERE, not rely on each caller having
        # already bootstrapped Django before its own docker-routing check — a
        # caller that routes to Docker before its own ``ensure_django()`` call
        # (or a future caller that never adds one) would otherwise import
        # ``credential_config`` while Django is unconfigured and crash with
        # ``ImproperlyConfigured`` instead of failing loud with
        # ``CredentialError``. ``ensure_django()`` is idempotent, so this is a
        # no-op when the caller already configured Django.
        ensure_django()
        # Imported at call time (not module top) to keep the eval CLI import chain
        # Django-free — ``credential_config`` pulls in the routing models + settings,
        # which cannot be created before ``django.setup()`` (plain ``import teatree.cli``).
        from teatree.credential_config import resolve_eval_credential  # noqa: PLC0415 — deferred: lazy CLI import

        credential = resolve_eval_credential()
        credential.export()
        auth_env_vars = (credential.spec.env_var,)
    root = _repo_root()
    if not _image_present():
        build_code = _build_image(root)
        if build_code != 0:
            return build_code
    return _run_in_image(root, eval_args, auth_env_vars=auth_env_vars, artifacts_dir=artifacts_dir)
