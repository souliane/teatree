# test-path: cross-cutting
"""Per-overlay ``worktree_root`` resolution (regroup worktrees under one dir).

``config.worktree_root()`` resolves the DB-home ``ConfigSetting``
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
from teatree.utils.env import patched_environ


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
        self.enterContext(patch.object(Path, "home", return_value=self.home))
        # Scope every touched env key to the test via the shared ``patched_environ``
        # seam: sandbox HOME and pin the overlay, so each test starts from the
        # DB/default path — all restored on teardown.
        self.enterContext(
            patched_environ(
                {"HOME": str(self.home), "T3_OVERLAY_NAME": "myoverlay"},
            )
        )


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


class TestExplicitOverlayArgument(_WorktreeRootCase):
    """``overlay=`` names the overlay instead of inheriting the process's ambient one.

    The container venue resolves NO ambient overlay: ``T3_OVERLAY_NAME`` is unset,
    ``WORKDIR /home/teatree`` has no ``manage.py`` ancestor, and two overlays are
    registered so the single-overlay fallback never fires. The name then resolves
    to ``""`` and the per-overlay segment vanishes — which is how one ticket's
    worktrees came to be split across two roots.
    """

    def _unresolvable_ambient(self) -> None:
        self.enterContext(patched_environ({}, remove=("T3_OVERLAY_NAME",)))
        self.enterContext(patch("teatree.config.resolution._active_overlay_entry", return_value=None))

    def test_explicit_overlay_beats_unresolvable_ambient(self) -> None:
        self._unresolvable_ambient()

        assert worktree_root() == self.home / "workspace" / "t3-workspaces"
        assert worktree_root(overlay="other-overlay") == self.home / "workspace" / "t3-workspaces" / "other-overlay"

    def test_explicit_overlay_reads_that_overlays_db_scope(self) -> None:
        ConfigSetting.objects.set_value("workspace_dir", "/srv/named-ws", scope="named")

        assert worktree_root(overlay="named") == Path("/srv/named-ws")
        assert worktree_root() == self.home / "workspace" / "t3-workspaces" / "myoverlay"

    def test_empty_string_overlay_is_not_none(self) -> None:
        # A caller declaring itself overlay-LESS is not the same as one omitting
        # the argument: collapsing the two re-creates the bug at the parameter.
        ConfigSetting.objects.set_value("workspace_dir", "/srv/ambient-ws", scope="myoverlay")

        assert worktree_root(overlay="") == self.home / "workspace" / "t3-workspaces"
        assert worktree_root() == Path("/srv/ambient-ws")

    def test_omitted_overlay_is_unchanged(self) -> None:
        assert worktree_root(overlay=None) == worktree_root()
        assert worktree_root() == self.home / "workspace" / "t3-workspaces" / "myoverlay"
