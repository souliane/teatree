"""The publication privacy gate under an AMBIGUOUS multi-overlay registry.

An install that registers more than one overlay — the bundled ``t3-teatree``
alongside any host overlay — makes a bare ``get_overlay()`` raise
``ImproperlyConfigured: Multiple overlays found``. The gate used to swallow that
into ``([], [])`` — "no single overlay resolves, so there are no overlay rules to
lose" — which is FALSE here: two overlays are installed and BOTH their
``privacy_redact_terms`` / ``privacy_block_patterns`` silently vanished from a
public-target scan, letting content through that a configured rule marks private.

These tests run against the REAL entry-point registry with ``T3_OVERLAY_NAME``
deleted — the ambiguity a multi-overlay install actually has. ``tests/conftest.py``
pins ``T3_OVERLAY_NAME`` process-wide, which is exactly why the existing suite
never reproduced this; deleting it is load-bearing, not incidental.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from teatree.core.gates import privacy_gate
from teatree.core.gates.privacy_gate import overlay_privacy_rules, scan_outbound_text
from teatree.core.overlay_loader import get_all_overlays, get_overlay

# Synthetic, never a real redact term: these tests must not reproduce any
# genuinely-private vocabulary in a fixture, a log, or an assertion message.
SYNTHETIC_TERM = "ZZTESTCODENAME"
SYNTHETIC_BLOCK_PATTERN = r"zz-synthetic-block-\d+"
PUBLIC_TARGET = "souliane/teatree"


# Overlay resolution applies DB-home ``[overlays.<name>]`` overrides via ``load_config()``,
# so the class needs the DB.
class TestPrivacyGateMultiOverlay(TestCase):
    def setUp(self) -> None:
        # Put the process in the real ambiguity and assert it, rather than mocking it.
        # Two levers, both reproducing a genuine production shape: no ``T3_OVERLAY_NAME``
        # (the in-process MCP server sets none), and a cwd under no overlay's project tree
        # so ``get_overlay``'s ambient cwd tier finds nothing either. The ``assertRaises``
        # is the anti-vacuity assertion: if the ambiguity ever stops being reachable, these
        # tests fail loudly instead of silently passing.
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("T3_OVERLAY_NAME", None)

        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(Path(tmp_dir.name))

        self.overlay_names = sorted(get_all_overlays())
        if len(self.overlay_names) < 2:
            self.skipTest(f"needs a multi-overlay install to reproduce the ambiguity, got {self.overlay_names}")
        with pytest.raises(ImproperlyConfigured, match="Multiple overlays found"):
            get_overlay()

    def _seed_rules_on_every_overlay(self, names: list[str]) -> None:
        for index, name in enumerate(names):
            config = get_all_overlays()[name].config
            for attr, value in (
                ("privacy_redact_terms", [f"{SYNTHETIC_TERM}{index}"]),
                ("privacy_block_patterns", [SYNTHETIC_BLOCK_PATTERN]),
            ):
                patcher = patch.object(config, attr, value)
                patcher.start()
                self.addCleanup(patcher.stop)

    def test_ambiguous_registry_keeps_every_overlays_redact_terms(self) -> None:
        self._seed_rules_on_every_overlay(self.overlay_names)

        rules = overlay_privacy_rules()
        assert rules is not None
        redact, block = rules

        assert sorted(redact) == [f"{SYNTHETIC_TERM}{i}" for i in range(len(self.overlay_names))]
        assert block == [SYNTHETIC_BLOCK_PATTERN]

    def test_ambiguous_registry_refuses_public_publish_carrying_an_overlay_term(self) -> None:
        # The blast-radius test: with the rules dropped this scan came back CLEAN
        # and the body published. Both overlays' terms must fire on a public target.
        self._seed_rules_on_every_overlay(self.overlay_names)

        with patch.object(privacy_gate, "_target_is_public", lambda _repo, _forge: True):
            for index in range(len(self.overlay_names)):
                result = scan_outbound_text(
                    text=f"An ordinary note mentioning {SYNTHETIC_TERM}{index} in passing.",
                    target_repo=PUBLIC_TARGET,
                    forge="github",
                )
                assert result.refused
                assert any(match.pattern_name == f"redact:{SYNTHETIC_TERM}{index}" for match in result.matches)

    def test_ambiguous_registry_applies_overlay_block_patterns(self) -> None:
        self._seed_rules_on_every_overlay(self.overlay_names)

        with patch.object(privacy_gate, "_target_is_public", lambda _repo, _forge: True):
            result = scan_outbound_text(text="Body carrying zz-synthetic-block-42 inline.", target_repo=PUBLIC_TARGET)

        assert result.refused
        assert any(match.pattern_name == f"block:{SYNTHETIC_BLOCK_PATTERN}" for match in result.matches)

    def test_explicit_overlay_name_wins_over_the_union(self) -> None:
        # A caller that KNOWS its overlay gets exactly that overlay's rules — the
        # union is the ambiguity fallback, not the new default.
        self._seed_rules_on_every_overlay(self.overlay_names)

        rules = overlay_privacy_rules(self.overlay_names[0])
        assert rules is not None
        redact, _block = rules

        assert redact == [f"{SYNTHETIC_TERM}0"]
