"""Per-worktree record of the most recent pre-push gate run."""

from dataclasses import dataclass
from pathlib import Path
from typing import Self

from teatree.utils.git_run import run as git_read

_RECORD_NAME = "t3-push-gate-run"
_SIGNAL_EXIT_OFFSET = 128
_SHELL_EXIT_CODE_LIMIT = 256


def _optional_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class GateRunRecord:
    """One gate run, updated in place so a hard kill leaves its last reached stage."""

    started: int
    pid: int | None
    lock: str
    lock_wait_s: int | None
    bound: str
    stage: int | None
    finished: int | None
    rc: int | None

    @classmethod
    def read(cls, repo: str | Path, *, since: float) -> Self | None:
        """Read this worktree's record, ignoring missing, malformed, or stale runs."""
        path_value = git_read(
            repo=str(repo),
            args=["rev-parse", "--path-format=absolute", "--git-path", _RECORD_NAME],
        )
        if not path_value:
            return None
        try:
            raw = Path(path_value).read_text(encoding="utf-8")
        except OSError:
            return None
        values = dict(line.partition("=")[::2] for line in raw.splitlines() if "=" in line)
        started = _optional_int(values.get("started", ""))
        if started is None or started < since:
            return None
        return cls(
            started=started,
            pid=_optional_int(values.get("pid", "")),
            lock=values.get("lock", ""),
            lock_wait_s=_optional_int(values.get("lock_wait_s", "")),
            bound=values.get("bound", ""),
            stage=_optional_int(values.get("stage", "")),
            finished=_optional_int(values.get("finished", "")),
            rc=_optional_int(values.get("rc", "")),
        )

    @property
    def died_mid_run(self) -> bool:
        return self.finished is None

    @property
    def has_signal_exit_code(self) -> bool:
        return self.rc is not None and _SIGNAL_EXIT_OFFSET < self.rc < _SHELL_EXIT_CODE_LIMIT

    @property
    def was_interrupted(self) -> bool:
        return self.died_mid_run or self.has_signal_exit_code

    @property
    def summary(self) -> str:
        parts = [f"started={self.started}"]
        for key, value in (
            ("pid", self.pid),
            ("lock", self.lock),
            ("lock_wait_s", self.lock_wait_s),
            ("bound", self.bound),
            ("stage", self.stage),
            ("finished", self.finished),
            ("rc", self.rc),
        ):
            if value not in {None, ""}:
                parts.append(f"{key}={value}")
        return " ".join(parts)


__all__ = ["GateRunRecord"]
