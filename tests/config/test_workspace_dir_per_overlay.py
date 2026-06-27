# test-path: cross-cutting
"""Per-overlay ``workspace_dir`` resolution (regroup worktrees under one dir).

``config.workspace_dir()`` resolves, first match wins: the ``T3_WORKSPACE_DIR``
env var (or the ``settings.T3_WORKSPACE_DIR`` Django setting) as the explicit,
highest-precedence back-compat override; then the DB-home ``ConfigSetting``
``workspace_dir`` row (the active overlay's scope, then the global scope); then
the sound default ``~/workspace/t3-workspaces/<overlay>/``.

Integration-first: real ``ConfigSetting`` rows in the DB and real env, no mocks.
"""

import os
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.config import workspace_dir
from teatree.core.models import ConfigSetting


class _WorkspaceDirCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        # Snapshot the whole environ (restored on exit) so each test mutates it
        # freely. The default cases assert the env/DB/default tiers with a known
        # active overlay and no T3_WORKSPACE_DIR override.
        self.enterContext(patch.dict(os.environ))
        os.environ.pop("T3_WORKSPACE_DIR", None)
        os.environ["T3_OVERLAY_NAME"] = "myoverlay"


class TestDefault(_WorkspaceDirCase):
    def test_per_overlay_default_when_unset(self) -> None:
        assert workspace_dir() == Path.home() / "workspace" / "t3-workspaces" / "myoverlay"

    def test_default_per_overlay_differs_between_overlays(self) -> None:
        os.environ["T3_OVERLAY_NAME"] = "alpha"
        alpha = workspace_dir()
        os.environ["T3_OVERLAY_NAME"] = "beta"
        beta = workspace_dir()
        assert alpha != beta
        assert alpha.name == "alpha"
        assert beta.name == "beta"
        assert alpha.parent == beta.parent == Path.home() / "workspace" / "t3-workspaces"


class TestDbConfigSetting(_WorkspaceDirCase):
    def test_global_scope_row_overrides_default(self) -> None:
        ConfigSetting.objects.set_value("workspace_dir", "/srv/global-ws", scope="")
        assert workspace_dir() == Path("/srv/global-ws")

    def test_overlay_scope_row_beats_global_scope(self) -> None:
        ConfigSetting.objects.set_value("workspace_dir", "/srv/global-ws", scope="")
        ConfigSetting.objects.set_value("workspace_dir", "/srv/overlay-ws", scope="myoverlay")
        assert workspace_dir() == Path("/srv/overlay-ws")

    def test_stored_value_is_tilde_expanded(self) -> None:
        ConfigSetting.objects.set_value("workspace_dir", "~/elsewhere/ws", scope="")
        assert workspace_dir() == Path.home() / "elsewhere" / "ws"


class TestEnvOverrideWins(_WorkspaceDirCase):
    def test_env_var_beats_db_row(self) -> None:
        ConfigSetting.objects.set_value("workspace_dir", "/srv/overlay-ws", scope="myoverlay")
        os.environ["T3_WORKSPACE_DIR"] = "/from/env"
        assert workspace_dir() == Path("/from/env")

    def test_django_setting_beats_db_row(self) -> None:
        ConfigSetting.objects.set_value("workspace_dir", "/srv/overlay-ws", scope="myoverlay")
        with self.settings(T3_WORKSPACE_DIR="/from/django"):
            assert workspace_dir() == Path("/from/django")
