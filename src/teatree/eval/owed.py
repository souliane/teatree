"""The owed-scenario ledger: eval scenarios a lane could not run, carried until a run passes them.

A PR lane records what it parked, deferred past its cap, or lost to a usage limit and uploads it
as the ``eval-owed`` artifact. The nightly is the single writer of the cumulative
``eval-owed-ledger``: :class:`OwedHarvest` folds the previous ledger with the PR artifacts of
every PR merged since its mark, the nightly runs the oldest debt first, and only a passing run
settles an entry.
"""

import datetime as dt
import io
import json
import zipfile
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Self

OWED_LEDGER_VERSION = 1
LEDGER_FILENAME = "eval-owed-ledger.json"
PR_OWED_FILENAME = "eval-owed.json"
LEDGER_ARTIFACT = "eval-owed-ledger"
PR_OWED_ARTIFACT = "eval-owed"

PAGE_SIZE = 100
MAX_LISTING_PAGES = 20
FIRST_HARVEST_LOOKBACK = dt.timedelta(days=1)

type GhRunner = Callable[[Sequence[str]], bytes]


class OwedReason(StrEnum):
    PARKED = "parked"
    DEFERRED = "deferred"
    LIMIT = "limit"


_PARKING_REASONS = frozenset({OwedReason.PARKED, OwedReason.LIMIT})


class OwedLedgerError(ValueError):
    pass


class TruncatedListingError(OwedLedgerError):
    def __init__(self, what: str) -> None:
        super().__init__(
            f"more than {MAX_LISTING_PAGES * PAGE_SIZE} {what}: refusing a truncated harvest that would lose owed debt"
        )


@dataclass(frozen=True)
class _Fields:
    raw: Mapping[str, object]
    where: str

    @classmethod
    def of(cls, raw: object, where: str) -> Self:
        if not isinstance(raw, dict):
            msg = f"{where}: expected a JSON object, got {type(raw).__name__}"
            raise OwedLedgerError(msg)
        return cls(raw=raw, where=where)

    @classmethod
    def parse(cls, data: bytes | str, where: str) -> Self:
        return cls.of(_decode(data, where), where)

    def value(self, key: str) -> object:
        if key not in self.raw:
            msg = f"{self.where}: missing {key!r}"
            raise OwedLedgerError(msg)
        return self.raw[key]

    def _typed[T](self, key: str, kind: type[T]) -> T:
        value = self.value(key)
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            msg = f"{self.where}: {key!r} must be {kind.__name__}, got {value!r}"
            raise OwedLedgerError(msg)
        return value

    def text(self, key: str) -> str:
        value = self._typed(key, str).strip()
        if not value:
            msg = f"{self.where}: {key!r} is blank"
            raise OwedLedgerError(msg)
        return value

    def integer(self, key: str) -> int:
        return self._typed(key, int)

    def flag(self, key: str) -> bool:
        return self._typed(key, bool)

    def rows(self, key: str) -> list[object]:
        return self._typed(key, list)

    def nested(self, key: str) -> Self:
        return type(self).of(self.value(key), f"{self.where}.{key}")

    def moment(self, key: str) -> dt.datetime:
        return self._parse_moment(self.text(key), key)

    def optional_moment(self, key: str) -> dt.datetime | None:
        return None if self.value(key) is None else self.moment(key)

    def _parse_moment(self, text: str, key: str) -> dt.datetime:
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError as exc:
            msg = f"{self.where}: {key!r} is not an ISO-8601 datetime: {text!r}"
            raise OwedLedgerError(msg) from exc
        if parsed.tzinfo is None:
            msg = f"{self.where}: {key!r} has no timezone: {text!r}"
            raise OwedLedgerError(msg)
        return parsed.astimezone(dt.UTC)


@dataclass(frozen=True)
class OwedScenario:
    name: str
    reason: OwedReason
    source: str
    since: dt.datetime

    def to_payload(self) -> dict[str, str]:
        return {"name": self.name, "reason": self.reason.value, "source": self.source, "since": self.since.isoformat()}

    @classmethod
    def from_fields(cls, fields: _Fields) -> Self:
        reason = fields.text("reason")
        if reason not in OwedReason:
            msg = f"{fields.where}: unknown reason {reason!r}"
            raise OwedLedgerError(msg)
        return cls(
            name=fields.text("name"),
            reason=OwedReason(reason),
            source=fields.text("source"),
            since=fields.moment("since"),
        )


@dataclass(frozen=True)
class OwedLedger:
    scenarios: tuple[OwedScenario, ...] = ()
    harvested_through: dt.datetime | None = None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(entry.name for entry in self.scenarios))

    @property
    def is_parked(self) -> bool:
        return any(entry.reason in _PARKING_REASONS for entry in self.scenarios)

    def recorded(self, names: Iterable[str], *, reason: OwedReason, source: str, since: dt.datetime) -> "OwedLedger":
        fresh = dict.fromkeys(stripped for name in names if (stripped := name.strip()))
        return self.merged(
            OwedLedger(scenarios=tuple(OwedScenario(name, reason, source, since) for name in fresh)),
        )

    def merged(self, other: "OwedLedger") -> "OwedLedger":
        by_name = {entry.name: entry for entry in self.scenarios}
        for entry in other.scenarios:
            held = by_name.get(entry.name)
            if held is None or entry.since < held.since:
                by_name[entry.name] = entry
        marks = [mark for mark in (self.harvested_through, other.harvested_through) if mark is not None]
        return OwedLedger(
            scenarios=tuple(sorted(by_name.values(), key=lambda entry: entry.name)),
            harvested_through=max(marks, default=None),
        )

    def settled(self, passed: Iterable[str], known: Collection[str]) -> tuple["OwedLedger", tuple[str, ...]]:
        cleared = frozenset(passed)
        dropped = tuple(name for name in self.names if name not in known)
        kept = tuple(entry for entry in self.scenarios if entry.name not in cleared and entry.name in known)
        return replace(self, scenarios=kept), dropped

    def next_batch(self, cap: int, known: Collection[str]) -> tuple[str, ...]:
        if cap < 1:
            msg = f"cap must be at least 1, got {cap}"
            raise ValueError(msg)
        runnable = sorted(
            (entry for entry in self.scenarios if entry.name in known),
            key=lambda entry: (entry.since, entry.name),
        )
        return tuple(entry.name for entry in runnable[:cap])

    def to_json(self) -> str:
        payload = {
            "version": OWED_LEDGER_VERSION,
            "harvested_through": None if self.harvested_through is None else self.harvested_through.isoformat(),
            "scenarios": [entry.to_payload() for entry in sorted(self.scenarios, key=lambda entry: entry.name)],
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str | bytes) -> Self:
        fields = _Fields.parse(text, "owed ledger")
        if (version := fields.integer("version")) != OWED_LEDGER_VERSION:
            msg = f"owed ledger: version {version} is not {OWED_LEDGER_VERSION}"
            raise OwedLedgerError(msg)
        harvested_through = fields.optional_moment("harvested_through")
        scenarios = tuple(
            OwedScenario.from_fields(_Fields.of(row, f"owed ledger.scenarios[{index}]"))
            for index, row in enumerate(fields.rows("scenarios"))
        )
        names = [entry.name for entry in scenarios]
        if duplicates := sorted({name for name in names if names.count(name) > 1}):
            msg = f"owed ledger: duplicate scenario {', '.join(duplicates)}"
            raise OwedLedgerError(msg)
        return cls(scenarios=scenarios, harvested_through=harvested_through)


@dataclass(frozen=True)
class _Artifact:
    id: int
    created_at: dt.datetime
    expired: bool
    head_sha: str

    @classmethod
    def from_fields(cls, fields: _Fields) -> Self:
        return cls(
            id=fields.integer("id"),
            created_at=fields.moment("created_at"),
            expired=fields.flag("expired"),
            head_sha=fields.nested("workflow_run").text("head_sha"),
        )


@dataclass(frozen=True)
class _Pull:
    number: int
    head_sha: str
    merged_at: dt.datetime | None
    updated_at: dt.datetime

    @classmethod
    def from_fields(cls, fields: _Fields) -> Self:
        return cls(
            number=fields.integer("number"),
            head_sha=fields.nested("head").text("sha"),
            merged_at=fields.optional_moment("merged_at"),
            updated_at=fields.moment("updated_at"),
        )


@dataclass(frozen=True)
class OwedHarvest:
    gh: GhRunner
    repo: str
    warn: Callable[[str], None]

    def previous_ledger(self) -> OwedLedger | None:
        live = [artifact for artifact in self._artifacts(LEDGER_ARTIFACT, done=_any_live) if not artifact.expired]
        if not live:
            return None
        newest = max(live, key=lambda artifact: artifact.created_at)
        return OwedLedger.from_json(self._member(newest, LEDGER_FILENAME))

    def merged_pr_heads(self, since: dt.datetime) -> list[tuple[int, str]]:
        pulls: list[_Pull] = []
        for page in range(1, MAX_LISTING_PAGES + 1):
            endpoint = (
                f"repos/{self.repo}/pulls?state=closed&sort=updated&direction=desc&per_page={PAGE_SIZE}&page={page}"
            )
            rows = self._rows(endpoint, key=None)
            batch = [_Pull.from_fields(_Fields.of(row, endpoint)) for row in rows]
            pulls.extend(batch)
            if len(batch) < PAGE_SIZE or batch[-1].updated_at < since:
                return [(pull.number, pull.head_sha) for pull in pulls if pull.merged_at and pull.merged_at >= since]
        listing = "closed pull requests"
        raise TruncatedListingError(listing)

    def pr_owed(self, heads: Sequence[tuple[int, str]]) -> OwedLedger:
        if not heads:
            return OwedLedger()
        wanted = {sha for _, sha in heads}
        artifacts = self._artifacts(
            PR_OWED_ARTIFACT, done=lambda seen: wanted <= {artifact.head_sha for artifact in seen}
        )
        owed = OwedLedger()
        for number, sha in heads:
            candidates = [artifact for artifact in artifacts if artifact.head_sha == sha]
            if not candidates:
                continue
            newest = max(candidates, key=lambda artifact: artifact.created_at)
            if newest.expired:
                self.warn(
                    f"::warning::{PR_OWED_ARTIFACT} artifact for PR #{number} expired before the harvest; "
                    "its owed scenarios are lost"
                )
                continue
            owed = owed.merged(OwedLedger.from_json(self._member(newest, PR_OWED_FILENAME)))
        return owed

    def collect(self, now: dt.datetime) -> OwedLedger:
        previous = self.previous_ledger()
        if previous is None:
            previous = OwedLedger()
            self.warn(
                f"::notice::no live {LEDGER_ARTIFACT} artifact: first harvest, reading PRs merged since "
                f"{(now - FIRST_HARVEST_LOOKBACK).isoformat()}"
            )
        since = previous.harvested_through or now - FIRST_HARVEST_LOOKBACK
        harvested = previous.merged(self.pr_owed(self.merged_pr_heads(since)))
        return replace(harvested, harvested_through=now)

    def _artifacts(self, name: str, *, done: Callable[[list[_Artifact]], bool]) -> list[_Artifact]:
        seen: list[_Artifact] = []
        for page in range(1, MAX_LISTING_PAGES + 1):
            endpoint = f"repos/{self.repo}/actions/artifacts?name={name}&per_page={PAGE_SIZE}&page={page}"
            batch = [_Artifact.from_fields(_Fields.of(row, endpoint)) for row in self._rows(endpoint, key="artifacts")]
            seen.extend(batch)
            if len(batch) < PAGE_SIZE or done(seen):
                return seen
        listing = f"{name} artifacts"
        raise TruncatedListingError(listing)

    def _rows(self, endpoint: str, *, key: str | None) -> list[object]:
        payload = _decode(self.gh(["api", endpoint]), endpoint)
        if key is not None:
            return _Fields.of(payload, endpoint).rows(key)
        if not isinstance(payload, list):
            msg = f"{endpoint}: expected a JSON list, got {type(payload).__name__}"
            raise OwedLedgerError(msg)
        return payload

    def _member(self, artifact: _Artifact, filename: str) -> bytes:
        archive = self.gh(["api", f"repos/{self.repo}/actions/artifacts/{artifact.id}/zip"])
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as unpacked:
                return unpacked.read(filename)
        except (zipfile.BadZipFile, KeyError) as exc:
            msg = f"artifact {artifact.id}: no readable {filename} ({exc})"
            raise OwedLedgerError(msg) from exc


def _decode(data: bytes | str, where: str) -> object:
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"{where}: not valid JSON ({exc})"
        raise OwedLedgerError(msg) from exc


def _any_live(seen: list[_Artifact]) -> bool:
    return any(not artifact.expired for artifact in seen)
