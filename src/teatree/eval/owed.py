"""The owed-scenario ledger: eval scenarios a lane could not run, carried until a run passes them.

A PR lane records what it parked, deferred past its cap, or lost to a usage limit and uploads it
as the ``eval-owed`` artifact; each weekly and heal leg uploads its own ``eval-owed-<leg>``. The
nightly is the single writer of the cumulative ``eval-owed-ledger``: :class:`OwedHarvest` folds
the previous ledger with the PR artifacts of every PR merged since its mark and the leg artifacts,
the nightly runs the oldest debt first, and only a passing run settles an entry.
"""

import datetime as dt
import io
import json
import zipfile
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import cached_property
from typing import Self

OWED_LEDGER_VERSION = 1
LEDGER_FILENAME = "eval-owed-ledger.json"
PR_OWED_FILENAME = "eval-owed.json"
LEDGER_ARTIFACT = "eval-owed-ledger"
PR_OWED_ARTIFACT = "eval-owed"
LEG_OWED_PREFIX = f"{PR_OWED_ARTIFACT}-"
LEG_OWED_WORKFLOWS = frozenset({".github/workflows/eval.yml", ".github/workflows/eval-ci-heal.yml"})

PAGE_SIZE = 100
MAX_LISTING_PAGES = 20
FIRST_HARVEST_LOOKBACK = dt.timedelta(days=1)
PR_ARTIFACT_LOOKBACK = dt.timedelta(days=90)

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
    last_attempted: dt.datetime | None = None

    def to_payload(self) -> dict[str, str]:
        payload = {
            "name": self.name,
            "reason": self.reason.value,
            "source": self.source,
            "since": self.since.isoformat(),
        }
        if self.last_attempted is not None:
            payload["last_attempted"] = self.last_attempted.isoformat()
        return payload

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
            last_attempted=fields.optional_moment("last_attempted") if "last_attempted" in fields.raw else None,
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
                by_name[entry.name] = replace(
                    entry,
                    last_attempted=max(
                        (mark for mark in (entry.last_attempted, held.last_attempted if held else None) if mark),
                        default=None,
                    ),
                )
            elif held is not None and entry.last_attempted is not None:
                by_name[entry.name] = replace(
                    held, last_attempted=max(mark for mark in (held.last_attempted, entry.last_attempted) if mark)
                )
        marks = [mark for mark in (self.harvested_through, other.harvested_through) if mark is not None]
        return OwedLedger(
            scenarios=tuple(sorted(by_name.values(), key=lambda entry: entry.name)),
            harvested_through=max(marks, default=None),
        )

    def settled(
        self,
        passed: Iterable[str],
        known: Collection[str],
        *,
        attempted: Iterable[str] = (),
        now: dt.datetime | None = None,
    ) -> tuple["OwedLedger", tuple[str, ...]]:
        cleared = frozenset(passed)
        tried = frozenset(attempted)
        dropped = tuple(name for name in self.names if name not in known)
        attempted_at = now or dt.datetime.now(dt.UTC)
        kept = tuple(
            replace(entry, last_attempted=attempted_at) if entry.name in tried else entry
            for entry in self.scenarios
            if entry.name not in cleared and entry.name in known
        )
        return replace(self, scenarios=kept), dropped

    def next_batch(self, cap: int, known: Collection[str]) -> tuple[str, ...]:
        if cap < 1:
            msg = f"cap must be at least 1, got {cap}"
            raise ValueError(msg)
        runnable = sorted(
            (entry for entry in self.scenarios if entry.name in known),
            key=lambda entry: (entry.last_attempted or entry.since, entry.since, entry.name),
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
    name: str
    created_at: dt.datetime
    expired: bool
    head_sha: str
    head_branch: str
    head_repository_id: int

    @classmethod
    def from_fields(cls, fields: _Fields) -> Self:
        run = fields.nested("workflow_run")
        return cls(
            id=fields.integer("id"),
            name=fields.text("name"),
            created_at=fields.moment("created_at"),
            expired=fields.flag("expired"),
            head_sha=run.text("head_sha"),
            head_branch=run.text("head_branch"),
            head_repository_id=run.integer("head_repository_id"),
        )

    @property
    def is_leg_owed(self) -> bool:
        return self.name.startswith(LEG_OWED_PREFIX) and self.name != LEDGER_ARTIFACT


@dataclass(frozen=True)
class _Workflow:
    id: int
    path: str

    @classmethod
    def from_fields(cls, fields: _Fields) -> Self:
        return cls(id=fields.integer("id"), path=fields.text("path"))


@dataclass(frozen=True)
class _Run:
    id: int
    created_at: dt.datetime
    updated_at: dt.datetime
    head_sha: str
    head_branch: str

    @classmethod
    def from_fields(cls, fields: _Fields) -> Self:
        return cls(
            id=fields.integer("id"),
            created_at=fields.moment("created_at"),
            updated_at=fields.moment("updated_at"),
            head_sha=fields.text("head_sha"),
            head_branch=fields.text("head_branch"),
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
    first_harvest_lookback: dt.timedelta = FIRST_HARVEST_LOOKBACK

    @cached_property
    def repository_identity(self) -> tuple[int, str]:
        endpoint = f"repos/{self.repo}"
        fields = _Fields.parse(self.gh(["api", endpoint]), endpoint)
        return fields.integer("id"), fields.text("default_branch")

    def previous_ledger(self) -> OwedLedger | None:
        live = [artifact for artifact in self._artifacts(LEDGER_ARTIFACT, done=_any_live) if not artifact.expired]
        if not live:
            return None
        newest = max(live, key=lambda artifact: artifact.created_at)
        return OwedLedger.from_json(self._member(newest, LEDGER_FILENAME))

    def merged_pr_heads(self, since: dt.datetime) -> list[tuple[int, str]]:
        pulls = self._listing(
            "pulls?state=closed&sort=updated&direction=desc",
            None,
            _Pull.from_fields,
            what="closed pull requests",
            stop=lambda seen: seen[-1].updated_at < since,
        )
        return [(pull.number, pull.head_sha) for pull in pulls if pull.merged_at and pull.merged_at >= since]

    def pr_owed(self, heads: Sequence[tuple[int, str]], *, since: dt.datetime | None = None) -> OwedLedger:
        if not heads:
            return OwedLedger()
        wanted = {sha for _, sha in heads}
        artifacts = self._artifacts(
            PR_OWED_ARTIFACT,
            done=lambda seen: wanted <= {artifact.head_sha for artifact in seen},
            older_than=None if since is None else since - PR_ARTIFACT_LOOKBACK,
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

    def leg_owed(self, final_heads: Collection[str], since: dt.datetime) -> OwedLedger:
        """Default-branch legs owe what they uploaded after the mark; others only at a merged PR's final head."""
        repository_id, default_branch = self.repository_identity
        owed = OwedLedger()
        for run in self._leg_runs(older_than=since - PR_ARTIFACT_LOOKBACK):
            on_default_branch = run.head_branch == default_branch
            if (run.updated_at <= since) if on_default_branch else (run.head_sha not in final_heads):
                continue
            path = f"actions/runs/{run.id}/artifacts"
            for artifact in self._listing(path, "artifacts", _Artifact.from_fields, what=path):
                if artifact.head_repository_id != repository_id or not artifact.is_leg_owed:
                    continue
                if not on_default_branch or artifact.created_at > since:
                    owed = owed.merged(self._leg_ledger(artifact))
        return owed

    def collect(self, now: dt.datetime) -> OwedLedger:
        previous = self.previous_ledger()
        if previous is None:
            previous = OwedLedger()
            self.warn(
                f"::notice::no live {LEDGER_ARTIFACT} artifact: first harvest, reading PRs merged since "
                f"{(now - self.first_harvest_lookback).isoformat()}"
            )
        since = previous.harvested_through or now - self.first_harvest_lookback
        heads = self.merged_pr_heads(since)
        harvested = previous.merged(self.pr_owed(heads, since=since))
        harvested = harvested.merged(self.leg_owed({sha for _, sha in heads}, since))
        return replace(harvested, harvested_through=now)

    def _leg_runs(self, *, older_than: dt.datetime) -> list[_Run]:
        workflows = self._listing("actions/workflows", "workflows", _Workflow.from_fields, what="workflows")
        return [
            run
            for workflow in workflows
            if workflow.path in LEG_OWED_WORKFLOWS
            for run in self._listing(
                f"actions/workflows/{workflow.id}/runs",
                "workflow_runs",
                _Run.from_fields,
                what=f"{workflow.path} runs",
                stop=lambda seen: seen[-1].created_at < older_than,
            )
        ]

    def _leg_ledger(self, artifact: _Artifact) -> OwedLedger:
        if artifact.expired:
            self.warn(f"::warning::{artifact.name} artifact expired before the harvest; its owed scenarios are lost")
            return OwedLedger()
        return OwedLedger.from_json(self._member(artifact, None))

    def _artifacts(
        self, name: str, *, done: Callable[[list[_Artifact]], bool], older_than: dt.datetime | None = None
    ) -> list[_Artifact]:
        repository_id, default_branch = self.repository_identity

        def trusted(seen: list[_Artifact]) -> list[_Artifact]:
            return [
                artifact
                for artifact in seen
                if artifact.head_repository_id == repository_id
                and (name != LEDGER_ARTIFACT or artifact.head_branch == default_branch)
            ]

        rows = self._listing(
            f"actions/artifacts?name={name}",
            "artifacts",
            _Artifact.from_fields,
            what=f"{name} artifacts",
            stop=lambda seen: done(trusted(seen)) or (older_than is not None and seen[-1].created_at < older_than),
        )
        return trusted(rows)

    def _listing[T](
        self,
        path: str,
        key: str | None,
        parse: Callable[[_Fields], T],
        *,
        what: str,
        stop: Callable[[list[T]], bool] | None = None,
    ) -> list[T]:
        rows: list[T] = []
        separator = "&" if "?" in path else "?"
        for page in range(1, MAX_LISTING_PAGES + 1):
            endpoint = f"repos/{self.repo}/{path}{separator}per_page={PAGE_SIZE}&page={page}"
            batch = [parse(_Fields.of(row, endpoint)) for row in self._rows(endpoint, key=key)]
            rows.extend(batch)
            if len(batch) < PAGE_SIZE or (stop is not None and stop(rows)):
                return rows
        raise TruncatedListingError(what)

    def _rows(self, endpoint: str, *, key: str | None) -> list[object]:
        payload = _decode(self.gh(["api", endpoint]), endpoint)
        if key is not None:
            return _Fields.of(payload, endpoint).rows(key)
        if not isinstance(payload, list):
            msg = f"{endpoint}: expected a JSON list, got {type(payload).__name__}"
            raise OwedLedgerError(msg)
        return payload

    def _member(self, artifact: _Artifact, filename: str | None) -> bytes:
        archive = self.gh(["api", f"repos/{self.repo}/actions/artifacts/{artifact.id}/zip"])
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as unpacked:
                return unpacked.read(filename or _only_owed_member(artifact, unpacked.namelist()))
        except (zipfile.BadZipFile, KeyError) as exc:
            msg = f"artifact {artifact.id}: no readable {filename or 'owed JSON'} ({exc})"
            raise OwedLedgerError(msg) from exc


def _only_owed_member(artifact: _Artifact, members: list[str]) -> str:
    owed = [member for member in members if member.startswith(LEG_OWED_PREFIX) and member.endswith(".json")]
    if len(owed) != 1:
        msg = f"artifact {artifact.id}: expected one owed JSON member, got {len(owed)}"
        raise OwedLedgerError(msg)
    return owed[0]


def _decode(data: bytes | str, where: str) -> object:
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"{where}: not valid JSON ({exc})"
        raise OwedLedgerError(msg) from exc


def _any_live(seen: list[_Artifact]) -> bool:
    return any(not artifact.expired for artifact in seen)
