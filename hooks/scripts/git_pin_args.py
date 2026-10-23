"""The ``git`` config pins every stdlib-only hook probe passes, mirroring ``teatree.utils.git_pins`` for a hook verb."""

GIT_PINS: tuple[str, ...] = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null")
