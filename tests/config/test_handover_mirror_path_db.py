# test-path: cross-cutting
"""DB-home ``handover_mirror_path`` resolution off the ``ConfigSetting`` store.

``handover_mirror_path`` was tagged "read when the DB is unreachable", but its
pre-Django SessionStart reader now reads the canonical
sqlite via ``cold_reader`` (Django-free) — which fails open to
``_default_handover_mirror_path()``, the exact path ``write_mirror`` uses when
unset — so it is DB-home. The Django-side reader ``core.handover.mirror_path()``
resolves it via ``get_effective_settings`` (env → DB overlay → DB global →
default); an empty stored value means "unset" and falls back to the default.

Integration-first: real ``ConfigSetting`` rows and real env, no mocks. HOME /
``Path.home`` are sandboxed so the suite never touches the developer's real home.
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.config.settings import _default_handover_mirror_path, _parse_handover_mirror_path
from teatree.core.handover import mirror_path
from teatree.core.models import ConfigSetting


class _MirrorPathCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        sandbox = Path(tempfile.mkdtemp(prefix="teatree-handover-mirror-"))
        self.home = sandbox / "home"
        self.home.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(sandbox, ignore_errors=True))
        self.enterContext(patch.dict(os.environ))
        self.enterContext(patch.object(Path, "home", return_value=self.home))
        os.environ["HOME"] = str(self.home)
        os.environ.pop("XDG_STATE_HOME", None)
        os.environ.pop("T3_OVERLAY_NAME", None)


class TestDbConfigSetting(_MirrorPathCase):
    def test_no_row_falls_back_to_default_under_xdg_state(self) -> None:
        assert mirror_path() == self.home / ".local" / "state" / "teatree" / "handover" / "latest.md"

    def test_global_scope_row_overrides_default(self) -> None:
        ConfigSetting.objects.set_value("handover_mirror_path", "/srv/ho/latest.md", scope="")
        assert mirror_path() == Path("/srv/ho/latest.md")

    def test_overlay_scope_row_beats_global_scope(self) -> None:
        os.environ["T3_OVERLAY_NAME"] = "myoverlay"
        ConfigSetting.objects.set_value("handover_mirror_path", "/srv/global/latest.md", scope="")
        ConfigSetting.objects.set_value("handover_mirror_path", "/srv/overlay/latest.md", scope="myoverlay")
        assert mirror_path() == Path("/srv/overlay/latest.md")

    def test_stored_value_is_tilde_expanded(self) -> None:
        ConfigSetting.objects.set_value("handover_mirror_path", "~/elsewhere/ho.md", scope="")
        assert mirror_path() == self.home / "elsewhere" / "ho.md"

    def test_empty_stored_value_means_unset_and_falls_back_to_default(self) -> None:
        ConfigSetting.objects.set_value("handover_mirror_path", "", scope="")
        assert mirror_path() == _default_handover_mirror_path()


class TestParser(_MirrorPathCase):
    def test_empty_string_resolves_to_default(self) -> None:
        assert _parse_handover_mirror_path("") == _default_handover_mirror_path()

    def test_value_resolves_to_expanded_path(self) -> None:
        assert _parse_handover_mirror_path("~/x/y.md") == self.home / "x" / "y.md"
