"""Config pins that keep a planted ``core.fsmonitor`` / ``core.hooksPath`` from running in a control-plane git spawn.

The control plane runs git outside the agent sandbox, in checkouts an agent can write the config of. An
``fsmonitor`` command or a hooks directory planted there would run with the control plane's authority.
Stdlib-only, so the pin logic has no import cost for ``teatree.utils.run``.
"""

from collections.abc import Sequence
from pathlib import Path

_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"})
_GATE_VERBS = frozenset({"commit", "merge", "pull", "push"})
_READER_VERBS = frozenset({"config", "rev-parse"})
_FSMONITOR_OFF = ("-c", "core.fsmonitor=")
_HOOKS_OFF = ("-c", "core.hooksPath=/dev/null")


class GitPins:
    @classmethod
    def argv(cls, cmd: Sequence[str], *, hooks_path: str | None = None) -> list[str]:
        """Pin Git config; hook-bearing verbs require their trusted default hooks path."""
        if not cmd or Path(cmd[0]).name != "git":
            return list(cmd)
        verb = cls._verb(cmd)
        if verb in _GATE_VERBS:
            if hooks_path is None:
                msg = "hook-bearing git commands need the checkout's default hooks path"
                raise ValueError(msg)
            pins = (*_FSMONITOR_OFF, "-c", f"core.hooksPath={hooks_path}")
        elif verb in _READER_VERBS:
            pins = _FSMONITOR_OFF
        else:
            pins = (*_FSMONITOR_OFF, *_HOOKS_OFF)
        return [cmd[0], *pins, *cmd[1:]]

    @classmethod
    def needs_hooks(cls, cmd: Sequence[str]) -> bool:
        return bool(cmd and Path(cmd[0]).name == "git" and cls._verb(cmd) in _GATE_VERBS)

    @classmethod
    def common_dir_probe(cls, cmd: Sequence[str]) -> list[str]:
        verb_index = cls._verb_index(cmd)
        if verb_index is None:
            msg = "git command has no verb"
            raise ValueError(msg)
        return [
            cmd[0],
            *cmd[1:verb_index],
            *_FSMONITOR_OFF,
            *_HOOKS_OFF,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ]

    @staticmethod
    def _verb_index(cmd: Sequence[str]) -> int | None:
        index = 1
        while index < len(cmd):
            token = cmd[index]
            if token in _VALUE_OPTIONS:
                index += 2
            elif not token.startswith("-"):
                return index
            else:
                index += 1
        return None

    @classmethod
    def _verb(cls, cmd: Sequence[str]) -> str | None:
        index = cls._verb_index(cmd)
        return cmd[index] if index is not None else None
