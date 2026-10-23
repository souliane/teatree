"""The ``git`` config pins every stdlib-only hook probe passes, mirroring ``teatree.utils.git_pins`` for a hook verb."""

import shutil

GIT_PINS: tuple[str, ...] = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null")


def git_executable() -> str:
    path = shutil.which("git")
    if path is None:
        message = "git executable unavailable"
        raise FileNotFoundError(message)
    return path
