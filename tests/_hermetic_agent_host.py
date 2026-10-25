"""A host with no agent credentials: every location strict setup probes is redirected into a temp dir."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase

from teatree.utils.secrets import PassStore, use_store

ANTHROPIC_ENV = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


class HermeticHostCase(TestCase):
    """Developer and worker hosts hold real credentials, so nothing probed may reach them."""

    def setUp(self) -> None:
        super().setUp()
        root = Path(self.enterContext(TemporaryDirectory()))
        self.home = root / "home"
        self.codex_home = root / "codex"
        self.private_codex_home = root / "t3-codex"
        self.password_store = root / "store"
        for directory in (self.home, self.codex_home, self.private_codex_home, self.password_store):
            directory.mkdir()
        redirected = {
            "HOME": str(self.home),
            "CODEX_HOME": str(self.codex_home),
            "T3_CODEX_HOME": str(self.private_codex_home),
            "PASSWORD_STORE_DIR": str(self.password_store),
        }
        self.enterContext(patch.dict(os.environ, redirected))
        self.enterContext(use_store(PassStore()))
        for name in ANTHROPIC_ENV:
            os.environ.pop(name, None)
