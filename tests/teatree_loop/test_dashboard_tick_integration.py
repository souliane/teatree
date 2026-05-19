"""Integration test: ``run_tick`` records dashboard rows to the sidecar (#1005).

The sidecar is the ground-truth feed the ``t3 loop dashboard`` CLI reads.
This test wires a synthetic scanner through the real tick path and
asserts the JSONL line lands on disk.
"""

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from teatree.loop.scanners.base import ScanSignal
from teatree.loop.tick import TickRequest, run_tick


@dataclass(slots=True)
class _FixedScanner:
    name: str
    out: list[ScanSignal]

    def scan(self) -> list[ScanSignal]:
        return self.out


def test_tick_appends_to_tick_actions_sidecar(tmp_path: Path) -> None:
    scanner = _FixedScanner(
        name="fake",
        out=[
            ScanSignal(
                kind="my_pr.open",
                summary="MR 99 open",
                payload={
                    "iid": 99,
                    "url": "https://gitlab.example/acme/-/merge_requests/99",
                    "overlay": "acme",
                    "title": "Fix offer",
                },
            ),
        ],
    )

    sidecar = tmp_path / "tick-actions.jsonl"
    with patch("teatree.loop.dashboard.default_actions_path", return_value=sidecar):
        run_tick(
            TickRequest(scanners=[scanner]),
            statusline_path=tmp_path / "statusline.txt",
            now=dt.datetime(2026, 5, 19, 9, 0, tzinfo=dt.UTC),
        )

    assert sidecar.is_file()
    rows = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["ref"] == "!99"
    assert rows[0]["overlay"] == "acme"
    assert rows[0]["url"].endswith("/merge_requests/99")


def test_tick_uses_user_identity_aliases_for_dedup(tmp_path: Path) -> None:
    """Reassignment among the user's own aliases should NOT land in the sidecar."""
    scanner = _FixedScanner(
        name="reassign",
        out=[
            ScanSignal(
                kind="ticket.disposition_candidate",
                summary="reassigned",
                payload={
                    "reason": "unassigned",
                    "old_owner": "alice-gh",
                    "new_owners": ["souliane"],
                    "iid": 77,
                    "url": "https://gitlab.example/acme/-/issues/77",
                    "overlay": "acme",
                    "ticket_number": "77",
                },
            ),
        ],
    )

    sidecar = tmp_path / "tick-actions.jsonl"

    # Stub load_config so it returns user_identity_aliases — without touching
    # the real ~/.teatree.toml.
    from typing import ClassVar  # noqa: PLC0415

    class _FakeUser:
        user_identity_aliases: ClassVar[list[str]] = ["alice-gh", "souliane"]

    class _FakeCfg:
        user = _FakeUser()

    with (
        patch("teatree.loop.dashboard.default_actions_path", return_value=sidecar),
        patch("teatree.loop.tick.load_config", return_value=_FakeCfg()),
    ):
        run_tick(
            TickRequest(scanners=[scanner]),
            statusline_path=tmp_path / "statusline.txt",
            now=dt.datetime(2026, 5, 19, 9, 0, tzinfo=dt.UTC),
        )

    # Identity dedup → no rows recorded (and therefore no file).
    assert not sidecar.is_file()
