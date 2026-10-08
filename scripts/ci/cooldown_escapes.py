"""Check the dated `[tool.uv] exclude-newer-package` escapes from the dependency cooldown (BLUEPRINT §15).

An escape admits one package's release inside the `exclude-newer` window. It must be a whole-second UTC
timestamp, it expires once the window has caught up with it, and `--verify-lock` asks PyPI whether it
equals the escaped release's newest upload rounded UP to the next second: uv's cutoff is exclusive, so
an exact upload time drops that file from the lock. Stdlib only, like `lock_delta.py`.
"""

import argparse
import dataclasses
import json
import re
import sys
import tomllib
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

type FetchJson = Callable[[str], Mapping[str, Any]]

_WINDOW = re.compile(r"^(\d+) days?$")
_WHOLE_SECOND_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PYTHON_FLOOR = re.compile(r"^>=\s*(\d+)\.(\d+)")
_WHEEL_TAGS = re.compile(r"-(?P<python>[^-]+)-(?P<abi>[^-]+)-[^-]+\.whl$")
_INTERPRETER = re.compile(r"^(?P<impl>[a-z]+?)(?P<major>\d)(?P<minor>\d*)$")


class PolicyError(Exception):
    pass


class PypiUnreadableError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Escape:
    package: str
    cutoff: datetime

    @property
    def label(self) -> str:
        return f"{self.package} = {self.cutoff:%Y-%m-%dT%H:%M:%SZ}"


@dataclasses.dataclass(frozen=True)
class Cooldown:
    window: timedelta
    escapes: tuple[Escape, ...]

    @classmethod
    def from_pyproject(cls, uv: Mapping[str, Any]) -> "Cooldown":
        span = _WINDOW.match(str(uv.get("exclude-newer", "")))
        if span is None:
            msg = f"[tool.uv] exclude-newer must read 'N days', got {uv.get('exclude-newer')!r}"
            raise PolicyError(msg)
        entries = uv.get("exclude-newer-package", {})
        if not isinstance(entries, Mapping):
            msg = f"[tool.uv] exclude-newer-package must be a table, got {entries!r}"
            raise PolicyError(msg)
        problems: list[str] = []
        escapes: list[Escape] = []
        for package, value in entries.items():
            if not isinstance(value, str):
                problems.append(f"{package} = {value!r} never expires; use the dated cutoff")
            elif not _WHOLE_SECOND_UTC.match(value):
                problems.append(f"{package} = {value!r} must be a UTC timestamp at a whole second (…:SSZ)")
            else:
                try:
                    cutoff = datetime.fromisoformat(value)
                except ValueError:
                    problems.append(f"{package} = {value!r} is an invalid UTC timestamp")
                else:
                    escapes.append(Escape(package, cutoff))
        if problems:
            raise PolicyError("; ".join(problems))
        return cls(timedelta(days=int(span[1])), tuple(escapes))

    def expired(self, now: datetime) -> tuple[Escape, ...]:
        return tuple(escape for escape in self.escapes if now >= escape.cutoff + self.window)


@dataclasses.dataclass(frozen=True)
class LockedRelease:
    version: str
    files: frozenset[str]


class LockAudit:
    def __init__(self, lock: Mapping[str, Any], fetch_json: FetchJson) -> None:
        floor = _PYTHON_FLOOR.match(str(lock.get("requires-python", "")))
        if floor is None:
            msg = f"uv.lock requires-python {lock.get('requires-python')!r} has no '>=X.Y' floor to judge wheels by"
            raise PolicyError(msg)
        self._python_floor = (int(floor[1]), int(floor[2]))
        self._packages = lock.get("package", [])
        self._fetch_json = fetch_json

    def problems(self, escape: Escape) -> list[str]:
        releases = self._locked(escape.package)
        if not releases:
            return [f"{escape.label}: {escape.package} is not in uv.lock"]
        problems: list[str] = []
        newest = datetime.min.replace(tzinfo=UTC)
        for release in releases:
            uploads = self._uploads(escape.package, release.version)
            newest = max(newest, *uploads.values())
            missing = sorted(name for name in uploads if self._lock_python_can_run(name) and name not in release.files)
            if missing:
                problems.append(f"{escape.label}: uv.lock lacks {escape.package} {release.version} {missing}")
        expected = newest.replace(microsecond=0) + timedelta(seconds=1)
        if escape.cutoff != expected:
            problems.append(
                f"{escape.label}: the newest file was uploaded {newest.isoformat()}; "
                f"the cutoff is exclusive, so set {expected:%Y-%m-%dT%H:%M:%SZ}"
            )
        return problems

    def _locked(self, package: str) -> list[LockedRelease]:
        return [
            LockedRelease(
                str(entry["version"]),
                frozenset(
                    artifact["url"].rsplit("/", 1)[-1]
                    for artifact in [entry.get("sdist", {}), *entry.get("wheels", [])]
                    if "url" in artifact
                ),
            )
            for entry in self._packages
            if _canonical(str(entry["name"])) == _canonical(package)
        ]

    def _uploads(self, package: str, version: str) -> dict[str, datetime]:
        url = f"https://pypi.org/pypi/{package}/{version}/json"
        try:
            files = self._fetch_json(url)["urls"]
            uploads = {str(file["filename"]): datetime.fromisoformat(file["upload_time_iso_8601"]) for file in files}
        except (OSError, ValueError, KeyError, TypeError) as error:
            msg = f"could not read {url}: {error!r}"
            raise PypiUnreadableError(msg) from error
        if not uploads:
            msg = f"{url} lists no files"
            raise PypiUnreadableError(msg)
        return uploads

    def _lock_python_can_run(self, filename: str) -> bool:
        wheel = _WHEEL_TAGS.search(filename)
        if not filename.endswith(".whl") or wheel is None:
            return True
        for tag in wheel["python"].split("."):
            interpreter = _INTERPRETER.match(tag)
            if interpreter is None:
                return True
            if interpreter["major"] != "3":
                continue
            if interpreter["impl"] == "py" or wheel["abi"] == "abi3" or not interpreter["minor"]:
                return True
            if (3, int(interpreter["minor"])) >= self._python_floor:
                return True
        return False


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _fetch_json(url: str) -> Mapping[str, Any]:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 — a fixed https://pypi.org URL
        return json.load(response)


def _report(level: str, message: str) -> None:
    print(f"::{level}::{message}", file=sys.stderr)


def main(argv: Sequence[str] | None = None, *, now: datetime | None = None, fetch_json: FetchJson = _fetch_json) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--fail-on-expired", action="store_true", help="exit 1 on an escape the window caught up with")
    parser.add_argument("--verify-lock", action="store_true", help="check each escape against PyPI and uv.lock")
    args = parser.parse_args(argv)

    try:
        pyproject = tomllib.loads(args.pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        _report("error", f"cannot read {args.pyproject}: {error}")
        return 2
    try:
        cooldown = Cooldown.from_pyproject(pyproject.get("tool", {}).get("uv", {}))
    except PolicyError as error:
        _report("error", f"cooldown escapes refused: {error}")
        return 1

    failed = False
    for escape in cooldown.expired(now or datetime.now(UTC)):
        failed |= args.fail_on_expired
        _report(
            "error" if args.fail_on_expired else "warning",
            f"{escape.label} has expired: the {cooldown.window.days}-day window admits that release now; "
            "remove the entry from pyproject.toml and relock",
        )

    if args.verify_lock and cooldown.escapes:
        try:
            audit = LockAudit(tomllib.loads(args.lock.read_text(encoding="utf-8")), fetch_json)
            problems = [problem for escape in cooldown.escapes for problem in audit.problems(escape)]
        except (PypiUnreadableError, PolicyError, OSError, tomllib.TOMLDecodeError) as error:
            _report("error", f"cannot verify the escapes: {error}")
            return 2
        for problem in problems:
            _report("error", problem)
        failed |= bool(problems)

    print(f"{len(cooldown.escapes)} cooldown escape(s), {cooldown.window.days}-day window.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
