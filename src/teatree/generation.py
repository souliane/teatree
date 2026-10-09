"""Which immutable code generation this process runs — read from the image, never configured.

An image built by ``deploy/build-generation.sh`` bakes ``TEATREE_GENERATION=<commit sha>``;
a process started from a source checkout (a dev stack, a laptop, CI) has none and is the
legacy generation ``""``, which keeps every pre-generation behaviour.
"""

import os
import re

GENERATION_ENV = "TEATREE_GENERATION"
IMAGE_REPOSITORY = "teatree-factory"

_GENERATION_SHA = re.compile(r"[0-9a-f]{40}")
_SHORT_SHA_LENGTH = 12


def current_generation() -> str:
    return os.environ.get(GENERATION_ENV, "").strip()


def is_image_generation() -> bool:
    return bool(current_generation())


def is_generation_sha(value: str) -> bool:
    return _GENERATION_SHA.fullmatch(value) is not None


def short_sha(sha: str) -> str:
    return f"{sha:.{_SHORT_SHA_LENGTH}}"


def generation_image(sha: str) -> str:
    repository = os.environ.get("TEATREE_IMAGE_REPOSITORY", "").strip() or IMAGE_REPOSITORY
    return f"{repository}:{sha}"


def in_place_update_refusal() -> str:
    """Why this process must not move its own code, or ``""`` for a legacy checkout that may."""
    sha = current_generation()
    return f"image generation {short_sha(sha)}: code changes by roll, not in place" if sha else ""
