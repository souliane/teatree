"""A fake monotonic clock for the hook process's budget: it moves only when a test says time passed."""

from dataclasses import dataclass


@dataclass
class HookClock:
    now: float = 0.0

    def monotonic(self) -> float:
        return self.now
