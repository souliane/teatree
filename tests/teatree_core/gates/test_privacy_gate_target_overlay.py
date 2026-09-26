"""A public-target scan is attributed to the overlay that OWNS the target.

``scan_outbound_text`` resolved the overlay ambiently, so on a two-overlay install
``get_overlay()`` raised on every send and resolution fell through to the #1295 registry
union. Two costs: the raise-and-union ran per send rather than the owning overlay being
named, and the union's own fail-safe SWALLOWED a genuine per-overlay read failure into
``([], [])`` — so an unreadable owning overlay scanned a public target with the built-in
detectors only, the fail-OPEN the ``None`` contract exists to prevent.

The union stays the scan VOCABULARY (#1295: "unioning can only refuse MORE, never leak
more"); what the owning overlay adds is attribution and its fail-CLOSED read.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from teatree.core.gates import privacy_gate
from teatree.core.gates.privacy_gate import scan_outbound_text
from teatree.core.overlay_loader import get_all_overlays, get_overlay
from teatree.core.overlays.repo_ownership import owning_overlay_for_repo
from tests.teatree_core.gates._two_overlay_registry import register_a_sibling_overlay

SYNTHETIC_TERM = "ZZTESTCODENAME"
PUBLIC_TARGET = "souliane/teatree"
_FORGE = "github"


class _UnreadablePrivacyConfig:
    """The real config with ONLY its privacy fields raising — a genuine resolution FAILURE.

    Delegating the rest is load-bearing: ``owned_repos`` on the same object is what
    attributes the target to its overlay, so a blanket stand-in would make the target
    read as unowned and exercise the ambient path instead of the owning one.
    """

    def __init__(self, real: object) -> None:
        self._real = real

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)

    @property
    def privacy_redact_terms(self) -> list[str]:
        msg = "privacy rules unreadable"
        raise RuntimeError(msg)

    @property
    def privacy_block_patterns(self) -> list[str]:
        msg = "privacy rules unreadable"
        raise RuntimeError(msg)


class _AmbiguousRegistry(TestCase):
    """The real two-overlay ambiguity, asserted rather than mocked (mirrors the #1295 suite)."""

    def setUp(self) -> None:
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("T3_OVERLAY_NAME", None)

        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(Path(tmp_dir.name))

        register_a_sibling_overlay(self)
        self.overlay_names = sorted(get_all_overlays())
        assert len(self.overlay_names) >= 2, self.overlay_names
        with pytest.raises(ImproperlyConfigured, match="Multiple overlays found"):
            get_overlay()

        self.owner = owning_overlay_for_repo(PUBLIC_TARGET, forge=_FORGE)
        assert self.owner in self.overlay_names, f"{PUBLIC_TARGET} must resolve to one registered overlay"

        public = patch.object(privacy_gate, "_target_is_public", lambda _repo, _forge=None: True)
        public.start()
        self.addCleanup(public.stop)


class TestTheOwningOverlayIsNamed(_AmbiguousRegistry):
    def test_the_scan_asks_for_the_target_repos_overlay_not_the_ambient_one(self) -> None:
        asked: list[str] = []
        real = privacy_gate.overlay_privacy_rules

        def spy(overlay_name: str = "") -> tuple[list[str], list[str]] | None:
            asked.append(overlay_name)
            return real(overlay_name)

        with patch.object(privacy_gate, "overlay_privacy_rules", spy):
            scan_outbound_text(text="An ordinary note.", target_repo=PUBLIC_TARGET, forge=_FORGE)

        assert asked == [self.owner]


class TestAnUnreadableOwningOverlayFailsClosed(_AmbiguousRegistry):
    def test_the_public_scan_refuses_instead_of_degrading_to_the_builtins(self) -> None:
        overlay = get_all_overlays()[self.owner]
        unreadable = patch.object(overlay, "config", _UnreadablePrivacyConfig(overlay.config))
        unreadable.start()
        self.addCleanup(unreadable.stop)

        result = scan_outbound_text(text="An ordinary note.", target_repo=PUBLIC_TARGET, forge=_FORGE)

        assert result.refused
        assert [match.pattern_name for match in result.matches] == ["overlay-rules-unresolvable"]


class TestTheSiblingOverlaysTermsStillBind(_AmbiguousRegistry):
    def test_a_term_only_the_non_owning_overlay_declares_still_refuses(self) -> None:
        sibling = next(name for name in self.overlay_names if name != self.owner)
        seeded = patch.object(get_all_overlays()[sibling].config, "privacy_redact_terms", [SYNTHETIC_TERM])
        seeded.start()
        self.addCleanup(seeded.stop)

        result = scan_outbound_text(
            text=f"An ordinary note mentioning {SYNTHETIC_TERM} in passing.",
            target_repo=PUBLIC_TARGET,
            forge=_FORGE,
        )

        assert result.refused
        assert any(match.pattern_name == f"redact:{SYNTHETIC_TERM}" for match in result.matches)


class TestAnUnreadableSiblingOverlayFailsClosed(_AmbiguousRegistry):
    def test_the_public_scan_refuses_instead_of_dropping_the_siblings_terms(self) -> None:
        sibling = next(name for name in self.overlay_names if name != self.owner)
        overlay = get_all_overlays()[sibling]
        unreadable = patch.object(overlay, "config", _UnreadablePrivacyConfig(overlay.config))
        unreadable.start()
        self.addCleanup(unreadable.stop)

        result = scan_outbound_text(text="An ordinary note.", target_repo=PUBLIC_TARGET, forge=_FORGE)

        assert result.refused
        assert [match.pattern_name for match in result.matches] == ["overlay-rules-unresolvable"]
