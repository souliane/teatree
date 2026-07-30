# test-path: cross-cutting
"""``consolidation-registry.json``'s cold WRITER and Django READER agree (#3828).

The per-agent consolidation registry is a cross-tier artifact of the
#3499 / #3819 / #3826 shape: two independent resolvers, one file, no test
that could observe both at once.

Note the direction, which is the reverse of the naive reading: the
Django-free hook tier OWNS the file — :mod:`hooks.scripts.hook_router`
resolves its path, claims slots into it and releases them — while the
Django tier only READS it, in
:func:`teatree.loop.self_improve.detectors.dispatch_gap._consolidation_registry_holders`.
So the detector's "no-one is picking these tasks up" verdict is a verdict
about a file it does not write, parsed by a duplicated resolver.

This lane round-trips the artifact through both resolvers: claim through
the hook, read back through the detector, assert the same interpretation
— including the directory each tier resolves the file in, which is where
the two duplicates diverged (the Django reader ignored ``XDG_DATA_HOME``
while the hook honours it, so with an XDG sandbox set the detector read
an entirely different file and reported "no holder" against a registry
full of them — the #3499 failure verbatim).
"""

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from django.test import TestCase

import hooks.scripts.hook_router as router
from teatree.core.models import Session, Task, Ticket
from teatree.loop.self_improve.detectors import DispatchGapDetector, dispatch_gap

_AGENT = "agent-alpha"
_SESSION = "session-alpha"


def _pin_registry_dir(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    """Point BOTH tiers at *directory* the way production does — by env only."""
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(directory))


class TestConsolidationRegistryRoundTrip:
    """A slot claimed through the hook is the holder the detector reads back."""

    def test_cold_claim_is_visible_to_the_django_reader(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _pin_registry_dir(monkeypatch, tmp_path)

        assert router._claim_agent_consolidation_slot(_AGENT, _SESSION) is True

        assert dispatch_gap._consolidation_registry_holders() == [_AGENT]

    def test_cold_release_is_visible_to_the_django_reader(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_registry_dir(monkeypatch, tmp_path)
        router._claim_agent_consolidation_slot(_AGENT, _SESSION)

        router._release_agent_consolidation_slot(_SESSION)

        assert dispatch_gap._consolidation_registry_holders() == []

    def test_every_claimed_agent_is_read_back_exactly_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The keyspace contract: the hook keys by ``agent_id`` and the reader
        # interprets the TOP-LEVEL keys as the holder list. A writer that moved
        # the agents under a nesting level would still produce a parseable dict.
        _pin_registry_dir(monkeypatch, tmp_path)
        for index in range(3):
            router._claim_agent_consolidation_slot(f"agent-{index}", f"session-{index}")

        assert sorted(dispatch_gap._consolidation_registry_holders()) == ["agent-0", "agent-1", "agent-2"]

    def test_the_reader_sees_the_file_the_writer_actually_wrote(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Filename parity: both duplicates must name the same leaf. Asserted
        # against the file on disk rather than against either tier's constant,
        # so a rename on one side alone cannot pass.
        _pin_registry_dir(monkeypatch, tmp_path)
        router._claim_agent_consolidation_slot(_AGENT, _SESSION)

        written = sorted(path.name for path in tmp_path.iterdir() if path.suffix == ".json")
        assert written == ["consolidation-registry.json"]
        stored = json.loads((tmp_path / "consolidation-registry.json").read_text(encoding="utf-8"))
        assert list(stored) == dispatch_gap._consolidation_registry_holders()


class TestConsolidationRegistryDirectoryParity:
    """Both tiers resolve the registry's DIRECTORY from the same environment."""

    def test_xdg_data_home_moves_both_tiers_together(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The live divergence this lane was built for: with no
        # ``T3_LOOP_REGISTRY_DIR`` the hook resolves ``$XDG_DATA_HOME/teatree``
        # while the reader resolved ``~/.local/share/teatree`` unconditionally.
        monkeypatch.delenv("T3_LOOP_REGISTRY_DIR", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        assert router._claim_agent_consolidation_slot(_AGENT, _SESSION) is True
        assert (tmp_path / "xdg" / "teatree" / "consolidation-registry.json").is_file()

        assert dispatch_gap._consolidation_registry_holders() == [_AGENT]

    def test_the_override_wins_over_xdg_in_both_tiers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        override = tmp_path / "override"
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(override))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        router._claim_agent_consolidation_slot(_AGENT, _SESSION)

        assert (override / "consolidation-registry.json").is_file()
        assert not (tmp_path / "xdg" / "teatree" / "consolidation-registry.json").exists()
        assert dispatch_gap._consolidation_registry_holders() == [_AGENT]


class DispatchGapReadsTheColdWriterTests(TestCase):
    """The detector's verdict follows the hook's claims, end to end."""

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory(prefix="consolidation-parity-")
        self.addCleanup(scratch.cleanup)
        self.registry_dir = Path(scratch.name)
        patcher = mock.patch.dict(os.environ, {"T3_LOOP_REGISTRY_DIR": str(self.registry_dir)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _pending_task(self) -> Task:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/1")
        session = Session.objects.create(ticket=ticket, agent_id=_AGENT)
        return Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.PENDING)

    def test_a_cold_claim_silences_the_detector_and_its_release_wakes_it(self) -> None:
        self._pending_task()

        assert DispatchGapDetector().detect(), "no holder yet ⇒ the smell must fire"

        router._claim_agent_consolidation_slot(_AGENT, _SESSION)
        assert DispatchGapDetector().detect() == []

        router._release_agent_consolidation_slot(_SESSION)
        assert DispatchGapDetector().detect()
