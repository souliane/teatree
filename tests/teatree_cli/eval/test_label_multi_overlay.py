"""``t3 eval label add`` under an AMBIGUOUS multi-overlay registry.

The public-corpus guard resolved the overlay ambiently and swallowed
``ImproperlyConfigured`` into ``([], [])``. On an install that registers more
than one overlay that raise is permanent, so every overlay's
``privacy_redact_terms`` / ``privacy_block_patterns`` silently vanished from the
scan and only :data:`_PUBLIC_CORPUS_BLOCK_PATTERNS` remained. The guard has no
fail-CLOSED tier for that case, so a capture carrying an overlay term and
nothing else was COPIED INTO THE PUBLIC CORPUS — fail OPEN, on a public path.

The existing suite could not catch it: its no-overlay test asserts a refusal
driven by a built-in host-path pattern, which fires whether or not the overlay
rules survive. These tests use a body with NO built-in trigger, so the refusal
can only come from an overlay rule, and they run against the REAL entry-point
registry with ``T3_OVERLAY_NAME`` deleted and the cwd outside any overlay
project — the ambiguity is asserted, not mocked.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase
from typer.testing import CliRunner

from teatree.cli import app
from teatree.core.overlay_loader import get_all_overlays, get_overlay
from tests.teatree_cli.eval.test_label import _assistant_bash, _record, _write_session

# Synthetic, never a real redact term, and deliberately free of anything the
# always-on public-corpus block set would catch on its own.
SYNTHETIC_TERM = "ZZTESTCODENAME"


class TestLabelAddMultiOverlay(TestCase):
    def setUp(self) -> None:
        # Put the process in the real ambiguity and assert it, rather than mocking it.
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("T3_OVERLAY_NAME", None)

        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tmp_path = Path(tmp_dir.name)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.tmp_path)

        self.overlay_names = sorted(get_all_overlays())
        if len(self.overlay_names) < 2:
            self.skipTest(f"needs a multi-overlay install to reproduce the ambiguity, got {self.overlay_names}")
        with pytest.raises(ImproperlyConfigured, match="Multiple overlays found"):
            get_overlay()

        self.home = self.tmp_path / "home"
        self.corpus_dir = self.tmp_path / "corpus"
        os.environ["HOME"] = str(self.home)

    def _seed_terms_on_every_overlay(self, names: list[str]) -> None:
        for index, name in enumerate(names):
            config = get_all_overlays()[name].config
            for attr, value in (("privacy_redact_terms", [f"{SYNTHETIC_TERM}{index}"]), ("privacy_block_patterns", [])):
                patcher = patch.object(config, attr, value)
                patcher.start()
                self.addCleanup(patcher.stop)

    def test_overlay_term_still_refuses_the_public_corpus_copy(self) -> None:
        self._seed_terms_on_every_overlay(self.overlay_names)

        for index in range(len(self.overlay_names)):
            session_id = f"sess-ambiguous-{index}"
            _record(session_id)
            _write_session(self.home, session_id, _assistant_bash(f"echo touching {SYNTHETIC_TERM}{index} here"))

            result = CliRunner().invoke(app, ["eval", "label", "add", session_id, "--dir", str(self.corpus_dir)])

            assert result.exit_code == 1, result.output
            assert "REFUSED" in result.output
            assert not self.corpus_dir.exists() or not list(self.corpus_dir.glob("*"))

    def test_clean_capture_is_still_scaffolded(self) -> None:
        # The union must not turn into a blanket refusal: an ordinary capture
        # still lands, so the guard stays usable rather than merely loud.
        self._seed_terms_on_every_overlay(self.overlay_names)
        _record("sess-ambiguous-clean")
        _write_session(self.home, "sess-ambiguous-clean", _assistant_bash("ls"))

        result = CliRunner().invoke(
            app,
            ["eval", "label", "add", "sess-ambiguous-clean", "--dir", str(self.corpus_dir), "--entry-id", "ok_entry"],
        )

        assert result.exit_code == 0, result.output
        assert (self.corpus_dir / "ok_entry.session.jsonl").is_file()
