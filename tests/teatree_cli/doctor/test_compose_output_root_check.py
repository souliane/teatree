"""The ``_check_compose_output_root_pinned`` doctor probe (souliane/teatree#3641).

Functional: a real ``deploy/docker-compose.yml`` under a tmp clone root is read
through the whole chain (``compose_path`` → ``services_missing_output_root``), so
the WARN/OK/degrade branches are exercised end to end rather than stubbed.
"""

import io
import tempfile
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from django.test import TestCase

from teatree.cli.doctor.checks_loop import _check_compose_output_root_pinned


def _echoes(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


class ComposeOutputRootCheckTest(TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clone = Path(tmp.name)

    def _write_compose(self, body: str) -> None:
        compose = self.clone / "deploy" / "docker-compose.yml"
        compose.parent.mkdir(parents=True, exist_ok=True)
        compose.write_text(body, encoding="utf-8")

    def _patch_clone(self, value: Path | None) -> None:
        patch = mock.patch(
            "teatree.cli.doctor.self_heal._Probe.runtime_clone_root",
            staticmethod(lambda: value),
        )
        patch.start()
        self.addCleanup(patch.stop)

    def test_no_runtime_clone_degrades_to_ok(self) -> None:
        self._patch_clone(None)
        ok, out = _echoes(_check_compose_output_root_pinned)
        assert ok is True
        assert out == ""

    def test_all_services_pin_the_root_is_ok(self) -> None:
        self._patch_clone(self.clone)
        self._write_compose("services:\n  worker:\n    environment:\n      TMPDIR: /var/tmp\n")
        ok, out = _echoes(_check_compose_output_root_pinned)
        assert ok is True
        assert out == ""

    def test_a_service_missing_the_root_warns_and_names_it(self) -> None:
        self._patch_clone(self.clone)
        self._write_compose("services:\n  worker:\n    image: t3\n")
        ok, out = _echoes(_check_compose_output_root_pinned)
        assert ok is False
        assert "WARN" in out
        assert "worker" in out
        assert "TMPDIR" in out

    def test_a_crash_in_the_probe_degrades_to_ok(self) -> None:
        self._patch_clone(self.clone)
        # The probe imports this deferred, so patch it at its source module.
        boom = mock.patch("teatree.docker.output_root.services_missing_output_root", side_effect=RuntimeError("boom"))
        boom.start()
        self.addCleanup(boom.stop)
        ok, out = _echoes(_check_compose_output_root_pinned)
        assert ok is True
        assert "crashed" in out
