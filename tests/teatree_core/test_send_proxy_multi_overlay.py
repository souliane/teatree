"""Send-proxy redaction under an AMBIGUOUS multi-overlay registry.

``_redact_terms`` took an ``overlay`` argument and then resolved the overlay
AMBIENTLY (``get_overlay()`` with no name), using the argument only in a debug
log. In an install that registers two overlays that call raises
``ImproperlyConfigured: Multiple overlays found``, the broad ``except`` swallowed
it, the term list came back empty, and :func:`redact_payload` returned every
payload untouched — redaction silently no-opped on the one seam every outbound
artifact routes through.

These tests run against the REAL entry-point registry with ``T3_OVERLAY_NAME``
deleted (``tests/conftest.py`` pins it process-wide, which is why the existing
suite never reproduced this).
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from teatree.core.overlay_loader import get_all_overlays, get_overlay
from teatree.core.send_proxy import REDACTION_PLACEHOLDER, redact_payload

# Synthetic, never a real redact term.
SYNTHETIC_TERM = "ZZTESTCODENAME"


# Overlay resolution applies DB-home ``[overlays.<name>]`` overrides via ``load_config()``,
# so the class needs the DB.
class TestSendProxyRedactionMultiOverlay(TestCase):
    def setUp(self) -> None:
        # Put the process in the real ambiguity and assert it, rather than mocking it.
        # No ``T3_OVERLAY_NAME`` (the in-process MCP server sets none) and a cwd under
        # no overlay's project tree, so ``get_overlay``'s ambient cwd tier finds nothing
        # either. The ``assertRaises`` is the anti-vacuity assertion.
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("T3_OVERLAY_NAME", None)

        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(tmp_dir.name)

        self.overlay_names = sorted(get_all_overlays())
        if len(self.overlay_names) < 2:
            self.skipTest(f"needs a multi-overlay install to reproduce the ambiguity, got {self.overlay_names}")
        with pytest.raises(ImproperlyConfigured, match="Multiple overlays found"):
            get_overlay()

    def _seed_terms_on_every_overlay(self, names: list[str]) -> None:
        for index, name in enumerate(names):
            patcher = patch.object(
                get_all_overlays()[name].config,
                "privacy_redact_terms",
                [f"{SYNTHETIC_TERM}{index}"],
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_blank_overlay_still_redacts_every_registered_overlays_terms(self) -> None:
        # The dominant production shape: ``route_forge_write`` builds its
        # ``SendRequest`` with no overlay, so ``overlay`` is "". Ambiguity must not
        # degrade that into "no terms" — every installed overlay's terms still apply.
        self._seed_terms_on_every_overlay(self.overlay_names)
        payload = " and ".join(f"{SYNTHETIC_TERM}{i}" for i in range(len(self.overlay_names)))

        redacted, matched = redact_payload(payload, overlay="")

        assert SYNTHETIC_TERM not in redacted
        assert redacted.count(REDACTION_PLACEHOLDER) == len(self.overlay_names)
        assert sorted(matched) == [f"{SYNTHETIC_TERM}{i}" for i in range(len(self.overlay_names))]

    def test_named_overlay_redacts_with_that_overlays_terms(self) -> None:
        # The reported defect proper: the ``overlay`` argument was accepted and
        # never threaded into resolution. A caller that names its overlay gets its terms.
        self._seed_terms_on_every_overlay(self.overlay_names)

        redacted, matched = redact_payload(f"leading {SYNTHETIC_TERM}0 trailing", overlay=self.overlay_names[0])

        assert redacted == f"leading {REDACTION_PLACEHOLDER} trailing"
        assert matched == (f"{SYNTHETIC_TERM}0",)

    def test_named_overlay_does_not_borrow_a_sibling_overlays_terms(self) -> None:
        self._seed_terms_on_every_overlay(self.overlay_names)

        _redacted, matched = redact_payload(f"mentions {SYNTHETIC_TERM}1 only", overlay=self.overlay_names[0])

        assert matched == ()
