# test-path: cross-cutting
"""Per-overlay ``worktree_root`` resolution (regroup worktrees under one dir).

``config.worktree_root()`` resolves, first match wins: the ``T3_WORKSPACE_DIR``
env var (or the ``settings.T3_WORKSPACE_DIR`` Django setting) as the explicit,
highest-precedence back-compat override; then the DB-home ``ConfigSetting``
``workspace_dir`` row (the active overlay's scope, then the global scope); then
the sound default ``~/workspace/t3-workspaces/<overlay>/``.

Integration-first: real ``ConfigSetting`` rows in the DB and real env, no mocks.
HOME / ``Path.home`` are sandboxed so the suite never reads or writes the
developer's real home — and the default resolver is PURE, so resolving it never
creates a directory.
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.config import worktree_root
from teatree.core.models import ConfigSetting


class _WorktreeRootCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        # Sandbox HOME *and* ``Path.home`` so every ``~`` resolution (the resolver
        # and the assertions alike) lands in a disposable temp dir — never the
        # developer's real home (the #regroup mkdir-in-getter fix removed the
        # side effect, but belt-and-suspenders keeps the suite host-independent).
        sandbox = Path(tempfile.mkdtemp(prefix="teatree-worktree-root-"))
        self.home = sandbox / "home"
        self.home.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(sandbox, ignore_errors=True))
        self.enterContext(patch.dict(os.environ))
        self.enterContext(patch.object(Path, "home", return_value=self.home))
        os.environ["HOME"] = str(self.home)
        os.environ.pop("T3_WORKSPACE_DIR", None)
        os.environ["T3_OVERLAY_NAME"] = "myoverlay"


class TestDefault(_WorktreeRootCase):
    def test_per_overlay_default_when_unset(self) -> None:
        assert worktree_root() == self.home / "workspace" / "t3-workspaces" / "myoverlay"

    def test_resolving_the_default_creates_no_directory(self) -> None:
        # The resolver is PURE — directory creation moved to the point of use
        # (ticket provisioning / relocate target), so a bare resolve never writes.
        resolved = worktree_root()
        assert not resolved.exists()

    def test_default_per_overlay_differs_between_overlays(self) -> None:
        os.environ["T3_OVERLAY_NAME"] = "alpha"
        alpha = worktree_root()
        os.environ["T3_OVERLAY_NAME"] = "beta"
        beta = worktree_root()
        assert alpha != beta
        assert alpha.name == "alpha"
        assert beta.name == "beta"
        assert alpha.parent == beta.parent == self.home / "workspace" / "t3-workspaces"


class TestDbConfigSetting(_WorktreeRootCase):
    def test_global_scope_row_overrides_default(self) -> None:
        ConfigSetting.objects.set_value("workspace_dir", "/srv/global-ws", scope="")
        assert worktree_root() == Path("/srv/global-ws")

    def test_overlay_scope_row_beats_global_scope(self) -> None:
        ConfigSetting.objects.set_value("workspace_dir", "/srv/global-ws", scope="")
        ConfigSetting.objects.set_value("workspace_dir", "/srv/overlay-ws", scope="myoverlay")
        assert worktree_root() == Path("/srv/overlay-ws")

    def test_stored_value_is_tilde_expanded(self) -> None:
        ConfigSetting.objects.set_value("workspace_dir", "~/elsewhere/ws", scope="")
        assert worktree_root() == self.home / "elsewhere" / "ws"


class TestEnvOverrideWins(_WorktreeRootCase):
    def test_env_var_beats_db_row(self) -> None:
        ConfigSetting.objects.set_value("workspace_dir", "/srv/overlay-ws", scope="myoverlay")
        os.environ["T3_WORKSPACE_DIR"] = "/from/env"
        assert worktree_root() == Path("/from/env")

    def test_django_setting_beats_db_row(self) -> None:
        ConfigSetting.objects.set_value("workspace_dir", "/srv/overlay-ws", scope="myoverlay")
        with self.settings(T3_WORKSPACE_DIR="/from/django"):
            assert worktree_root() == Path("/from/django")
