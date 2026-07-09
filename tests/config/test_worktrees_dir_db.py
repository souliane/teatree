# test-path: cross-cutting
"""DB-home ``worktrees_dir`` resolution off the ``ConfigSetting`` store.

``worktrees_dir`` was tagged "needed to open the DB", but Django ``settings.py``
hardcodes ``TIME_ZONE`` and configures ``DATABASES`` without reading it — so it is
DB-home, resolved Django-side by
``config.worktrees_dir()`` exactly like ``workspace_dir`` / ``worktree_root()``:
the ``T3_WORKTREES_DIR`` env/Django override first, then the ``ConfigSetting``
row (overlay scope, then global), then the dataclass default.

Integration-first: real ``ConfigSetting`` rows and real env, no mocks. HOME /
``Path.home`` are sandboxed so the suite never touches the developer's real home.
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.config import worktrees_dir
from teatree.core.models import ConfigSetting


class _WorktreesDirCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        sandbox = Path(tempfile.mkdtemp(prefix="teatree-worktrees-dir-"))
        self.home = sandbox / "home"
        self.home.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(sandbox, ignore_errors=True))
        self.enterContext(patch.dict(os.environ))
        self.enterContext(patch.object(Path, "home", return_value=self.home))
        os.environ["HOME"] = str(self.home)
        os.environ.pop("T3_WORKTREES_DIR", None)
        os.environ.pop("T3_OVERLAY_NAME", None)


class TestDbConfigSetting(_WorktreesDirCase):
    def test_global_scope_row_overrides_default(self) -> None:
        ConfigSetting.objects.set_value("worktrees_dir", "/srv/wt", scope="")
        assert worktrees_dir() == Path("/srv/wt")

    def test_overlay_scope_row_beats_global_scope(self) -> None:
        os.environ["T3_OVERLAY_NAME"] = "myoverlay"
        ConfigSetting.objects.set_value("worktrees_dir", "/srv/global-wt", scope="")
        ConfigSetting.objects.set_value("worktrees_dir", "/srv/overlay-wt", scope="myoverlay")
        assert worktrees_dir() == Path("/srv/overlay-wt")

    def test_stored_value_is_tilde_expanded(self) -> None:
        ConfigSetting.objects.set_value("worktrees_dir", "~/elsewhere/wt", scope="")
        assert worktrees_dir() == self.home / "elsewhere" / "wt"


class TestEnvOverrideWins(_WorktreesDirCase):
    def test_django_setting_beats_db_row(self) -> None:
        ConfigSetting.objects.set_value("worktrees_dir", "/srv/wt", scope="")
        with self.settings(T3_WORKTREES_DIR="/from/django/wt"):
            assert worktrees_dir() == Path("/from/django/wt")
