"""Single master Docker base-image build and reuse.

Teatree builds each ``BaseImageConfig`` once from the master clone's lockfile
and tags it with the single master tag ``{image_name}:base`` — NOT per-lockfile.
Worktrees reuse that one image through a ``.:/app:rw`` volume mount, so code
changes never trigger a rebuild, and dependency drift is reconciled at
container start by the overlay's entrypoint (``uv sync`` against the branch's
lockfile) — that's out of scope here.

The image is (re)built only when it is ABSENT or BROKEN. A worktree provision
never triggers a build beyond this absent/broken check — build once, reuse by
all.
"""

from teatree.types import BaseImageConfig
from teatree.utils.run import run_allowed_to_fail, run_checked

__all__ = ["ensure_base_image"]


def _is_broken(tag: str) -> bool:
    """Return True when ``tag`` is present but its config Id cannot be resolved.

    ``docker image inspect TAG`` reports the tag exists, but the cheap Id probe
    (``--format '{{.Id}}'``) returns an empty Id or a non-zero rc — corrupt
    metadata or an interrupted build. "Broken" is deliberately narrow:
    inspect-resolvable-Id, NOT a container-run healthcheck, so a good image is
    never torn down by a false negative.
    """
    probe = run_allowed_to_fail(
        ["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
        expected_codes=None,
    )
    return probe.returncode != 0 or not probe.stdout.strip()


def ensure_base_image(cfg: BaseImageConfig) -> str:
    """Build ``cfg`` into the single master-tagged image if absent or broken; return the tag.

    Idempotent: a present, Id-resolvable image is a no-op beyond the
    ``docker image inspect`` probe. An absent image (inspect rc != 0) is built.
    A broken image (inspect reports the tag but cannot resolve its Id) is
    ``docker rmi -f``'d and rebuilt. Raises ``CommandFailedError`` if the build
    itself fails.
    """
    tag = cfg.image_tag()
    present = run_allowed_to_fail(
        ["docker", "image", "inspect", tag],
        expected_codes=(0, 1),
    )
    if present.returncode == 0:
        if not _is_broken(tag):
            return tag
        run_checked(["docker", "rmi", "-f", tag])

    build_cmd: list[str] = [
        "docker",
        "build",
        "-f",
        str(cfg.build_context / cfg.dockerfile),
        "-t",
        tag,
    ]
    for key, value in cfg.build_args.items():
        build_cmd.extend(["--build-arg", f"{key}={value}"])
    build_cmd.append(str(cfg.build_context))
    run_checked(build_cmd, cwd=cfg.build_context)
    return tag
