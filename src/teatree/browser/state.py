from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SessionFiles:
    root: Path

    @property
    def endpoint(self) -> Path:
        return self.root / "endpoint.json"

    @property
    def pid(self) -> Path:
        return self.root / "keeper.pid"

    @property
    def events(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def stop(self) -> Path:
        return self.root / "stop"

    @property
    def error(self) -> Path:
        return self.root / "keeper-error.txt"

    @property
    def last_used(self) -> Path:
        return self.root / "last-used"

    @property
    def lock(self) -> Path:
        return self.root / "lock"

    @property
    def log(self) -> Path:
        return self.root / "keeper.log"

    @property
    def profile(self) -> Path:
        return self.root / "profile"

    @property
    def artifacts(self) -> Path:
        return self.root / "inspect"
