"""Opt-in real model nonce exchange for the three previously unproved pairings."""

import os

import pytest
from django.test import TestCase

from tests.integration.live_control.live_proof import run_pair


@pytest.mark.integration
@pytest.mark.timeout(360)
@pytest.mark.usefixtures("pass_store")
class LivePairingsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        if os.environ.get("T3_LIVE_PAIRINGS") != "1":
            self.skipTest("set T3_LIVE_PAIRINGS=1 for authenticated model pairings")

    def test_codex_to_claude_nonce_reaches_recipient_output(self) -> None:
        run_pair("codex_app_server", "claude_sdk")

    def test_codex_to_codex_nonce_reaches_recipient_output(self) -> None:
        run_pair("codex_app_server", "codex_app_server")

    def test_claude_to_claude_nonce_reaches_recipient_output(self) -> None:
        run_pair("claude_sdk", "claude_sdk")
