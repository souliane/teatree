"""A dispatch pinned by agent_harness=codex_app_server skips the availability probe, so the harness refuses on open."""

import asyncio
import shutil
import subprocess
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from django.test import TestCase

from teatree.agents import codex_app_server_options
from teatree.agents.codex_app_server_options import CONTAINER_IS_SANDBOX_ENV
from teatree.agents.codex_auth_cache import CODEX_AUTH_PASS_ENTRY, CodexAuthCache, CodexAuthCacheError
from teatree.agents.harness_dispatch import resolve_dispatch_harness
from teatree.agents.harness_registry import HarnessFallbackError, HarnessFallbackKind
from teatree.core.models import Session, Task
from teatree.core.models.config_setting import ConfigSetting
from tests.factories import planned_ticket


def _refusal_on_open(task: Task, phase: str, cwd: Path) -> HarnessFallbackError:
    dispatch = resolve_dispatch_harness(task, phase=phase)
    assert dispatch.name == "codex_app_server"

    async def enter() -> None:
        async with dispatch.harness.open(ClaudeAgentOptions(cwd=str(cwd), permission_mode="bypassPermissions")):
            pytest.fail("the harness opened")

    with pytest.raises(HarnessFallbackError) as refused:
        asyncio.run(enter())
    return refused.value


class TestAPinnedCodexDispatchIsRefusedWhereItOpens(TestCase):
    def _pin(self, phase: str) -> Task:
        ConfigSetting.objects.set_value("agent_harness", "codex_app_server")
        ticket = planned_ticket()
        return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase=phase)

    def _container(self, *, opted_in: bool) -> AbstractContextManager[Any]:
        return patch.multiple(
            codex_app_server_options,
            running_in_container=lambda: True,
            container_is_the_sandbox=lambda: opted_in,
        )

    def test_a_container_that_has_not_opted_in_is_refused(self) -> None:
        task = self._pin("coding")
        with (
            self._container(opted_in=False),
            patch("teatree.agents.codex_app_server.container_is_the_sandbox", return_value=False),
            patch("teatree.agents.codex_app_server.shutil.which", return_value="/usr/bin/codex"),
            tempfile.TemporaryDirectory() as directory,
        ):
            refusal = _refusal_on_open(task, "coding", Path(directory))

        assert refusal.kind is HarnessFallbackKind.ACCESS
        assert refusal.side_effects_started is False
        assert CONTAINER_IS_SANDBOX_ENV in str(refusal)

    def test_an_opted_in_container_with_a_user_execpolicy_rule_is_refused(self) -> None:
        task = self._pin("coding")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "codex-home"
            (home / "rules").mkdir(parents=True)
            (home / "rules" / "default.rules").write_text('prefix_rule(pattern = ["git"], decision = "allow")\n')
            with (
                self._container(opted_in=True),
                patch("teatree.agents.codex_app_server.container_is_the_sandbox", return_value=True),
                patch("teatree.agents.codex_app_server.shutil.which", return_value="/usr/bin/codex"),
                patch.dict("os.environ", {"T3_CODEX_HOME": str(home)}),
            ):
                refusal = _refusal_on_open(task, "coding", Path(directory))

        assert refusal.kind is HarnessFallbackKind.ACCESS
        assert "default.rules" in str(refusal)

    def test_an_architectural_review_that_starts_in_a_managed_main_clone_is_refused(self) -> None:
        task = self._pin("architectural_review")
        git = shutil.which("git")
        assert git is not None
        with tempfile.TemporaryDirectory() as directory:
            clone = Path(directory) / "clone"
            clone.mkdir()
            subprocess.run([git, "init", "-q", "-b", "main", str(clone)], check=True)
            subprocess.run(
                [git, "-C", str(clone), "remote", "add", "origin", "https://github.com/souliane/teatree.git"],
                check=True,
            )
            with (
                self._container(opted_in=True),
                patch("teatree.agents.codex_app_server.container_is_the_sandbox", return_value=True),
                patch("teatree.agents.codex_app_server.shutil.which", return_value="/usr/bin/codex"),
                patch.dict("os.environ", {"T3_REPO": str(clone), "T3_CODEX_HOME": str(Path(directory) / "home")}),
            ):
                refusal = _refusal_on_open(task, "architectural_review", clone)

        assert refusal.kind is HarnessFallbackKind.ACCESS
        assert "main clone" in str(refusal)

    def test_an_opted_in_container_without_rules_builds_a_harness_that_is_not_refused(self) -> None:
        task = self._pin("coding")
        with (
            self._container(opted_in=True),
            patch("teatree.agents.codex_app_server.container_is_the_sandbox", return_value=True),
            patch("teatree.agents.codex_app_server.shutil.which", return_value="/usr/bin/codex"),
            tempfile.TemporaryDirectory() as directory,
            patch.dict("os.environ", {"T3_CODEX_HOME": str(Path(directory) / "home")}),
            patch.object(
                CodexAuthCache,
                "hydrate",
                side_effect=CodexAuthCacheError.pass_read_failed(CODEX_AUTH_PASS_ENTRY),
            ),
        ):
            dispatch = resolve_dispatch_harness(task, phase="coding")
            assert dispatch.name == "codex_app_server"
            assert dispatch.harness.refusal is None

            async def enter() -> None:
                options = ClaudeAgentOptions(cwd=directory, permission_mode="bypassPermissions")
                async with dispatch.harness.open(options):
                    pytest.fail("auth failure must happen before Codex starts")

            with pytest.raises(HarnessFallbackError) as reached:
                asyncio.run(enter())

        assert reached.value.kind is HarnessFallbackKind.AUTH
