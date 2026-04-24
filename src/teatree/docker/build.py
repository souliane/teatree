"""Lockfile-keyed Docker base-image build and cache.

Teatree builds each ``BaseImageConfig`` once on the main repo and tags it as
``{image_name}:deps-{sha256(lockfile)[:12]}``.  Worktrees reuse the image
through a ``.:/app:rw`` volume mount, so code changes never trigger a rebuild.

The image is rebuilt only when the lockfile hash changes.  Dependency drift
inside a running container is expected to be handled by the overlay's
entrypoint (compare mounted lockfile to the copy baked into the image and
install on mismatch) — that's out of scope here.
"""

import hashlib

from teatree.types import BaseImageConfig
from teatree.utils.run import run_allowed_to_fail, run_checked

__all__ = ["ensure_base_image", "image_tag_for_lockfile"]


def image_tag_for_lockfile(cfg: BaseImageConfig) -> str:
    """Return ``{image_name}:deps-{sha256(lockfile)[:12]}`` for ``cfg``.

    Pure: only reads the lockfile bytes.  Safe to call during env-cache
    rendering and drift checks.
    """
    digest = hashlib.sha256((cfg.build_context / cfg.lockfile).read_bytes()).hexdigest()[:12]
    return f"{cfg.image_name}:deps-{digest}"


def ensure_base_image(cfg: BaseImageConfig) -> str:
    """Build ``cfg`` into a lockfile-tagged image if absent; return the tag.

    Idempotent: a second call with an unchanged lockfile is a no-op beyond
    the ``docker image inspect`` probe.  Raises ``CommandFailedError`` if the
    build itself fails.
    """
    tag = image_tag_for_lockfile(cfg)
    probe = run_allowed_to_fail(
        ["docker", "image", "inspect", tag],
        expected_codes=(0, 1),
    )
    if probe.returncode == 0:
        return tag

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
