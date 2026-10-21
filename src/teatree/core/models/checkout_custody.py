"""Durable checkout holder history since the last trusted state."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Self

from teatree.core.models.checkout_proof import CheckoutCustody, CheckoutTenure

_MAX_TENURES = 16
RELEASED_EXIT = "released"
ABANDONED_EXIT = "abandoned"


@dataclass(frozen=True)
class Tenure:
    holder: str
    session: str
    since: str
    ended: str = ""
    ended_at: str = ""
    namespace: str = ""

    def as_dict(self) -> CheckoutTenure:
        return CheckoutTenure(
            holder=self.holder,
            session=self.session,
            since=self.since,
            ended=self.ended,
            ended_at=self.ended_at,
            namespace=self.namespace,
        )


@dataclass(frozen=True)
class Custody:
    anchor: str
    tenures: tuple[Tenure, ...]

    @classmethod
    def from_extra(cls, extra: Mapping[str, Any]) -> Self | None:
        raw = extra.get("checkout_custody")
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("anchor"), str)
            or raw["anchor"] != extra.get("attested_state")
        ):
            return None
        entries = raw.get("tenures")
        if not isinstance(entries, list) or len(entries) > _MAX_TENURES:
            return None
        tenures: list[Tenure] = []
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("namespace", ""), str)
                or any(
                    not isinstance(entry.get(key), str) for key in ("holder", "session", "since", "ended", "ended_at")
                )
            ):
                return None
            if (
                not entry["holder"]
                or not entry["since"]
                or entry["ended"] not in {"", RELEASED_EXIT, ABANDONED_EXIT}
                or bool(entry["ended"]) != bool(entry["ended_at"])
            ):
                return None
            if tenures and not tenures[-1].ended:
                return None
            tenures.append(
                Tenure(
                    entry["holder"],
                    entry["session"],
                    entry["since"],
                    entry["ended"],
                    entry["ended_at"],
                    entry.get("namespace", ""),
                )
            )
        return cls(raw["anchor"], tuple(tenures))

    def as_dict(self) -> CheckoutCustody:
        return CheckoutCustody(anchor=self.anchor, tenures=[entry.as_dict() for entry in self.tenures])

    @property
    def prior_tenures_nested(self) -> bool:
        previous = self.tenures[:-1]
        return bool(previous) and all(entry.namespace for entry in previous)

    def opened(self, holder: str, session: str, now: datetime, *, renewal: bool, namespace: str = "") -> "Custody":
        entries = list(self.tenures)
        if entries and not entries[-1].ended:
            if renewal and (entries[-1].holder, entries[-1].session) == (holder, session):
                return self
            entries[-1] = replace(entries[-1], ended=ABANDONED_EXIT, ended_at=now.isoformat())
        entries.append(Tenure(holder, session, now.isoformat(), namespace=namespace))
        return Custody(self.anchor, tuple(entries))

    def closed(self, holder: str, session: str, now: datetime) -> "Custody":
        entries = list(self.tenures)
        if entries and not entries[-1].ended and (entries[-1].holder, entries[-1].session) == (holder, session):
            entries[-1] = replace(entries[-1], ended=RELEASED_EXIT, ended_at=now.isoformat())
        return Custody(self.anchor, tuple(entries))

    def without_open(self, holder: str, session: str) -> "Custody":
        entries = list(self.tenures)
        if entries and not entries[-1].ended and (entries[-1].holder, entries[-1].session) == (holder, session):
            entries.pop()
        return Custody(self.anchor, tuple(entries))

    @classmethod
    def reanchored(cls, digest: str, current: "Custody | None", holder: str, session: str) -> "Custody":
        last = current.tenures[-1] if current and current.tenures else None
        keep = (last,) if last and not last.ended and (last.holder, last.session) == (holder, session) else ()
        return cls(digest, keep)
