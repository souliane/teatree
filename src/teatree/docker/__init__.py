"""Docker helpers — base-image sharing, compose orchestration."""

from teatree.docker.build import ensure_base_image, image_tag_for_lockfile

__all__ = ["ensure_base_image", "image_tag_for_lockfile"]
