# test-path: cross-cutting — tests hooks/scripts/django_bootstrap.py, which has no src/teatree/ mirror.
"""A failed Django bootstrap must be AUDIBLE — a mute skip reads as a clean pass.

``run-hook.sh`` deliberately falls back to a version-floor-only interpreter with
no Django and prints nothing. On such a host every ORM-backed gate degrades to a
silent allow, and an operator inspecting the session sees a clean run and
concludes the gates passed. The bootstrap therefore names the missing capability
once per process, and the gates that degrade on it say SKIPPED rather than
nothing.
"""

import pytest

from hooks.scripts import django_bootstrap


@pytest.fixture(autouse=True)
def _reset_warn_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(django_bootstrap, "_MISSING_CAPABILITY_WARNED", False)


_NO_DJANGO = ModuleNotFoundError("No module named 'django'")


def _break_django_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``django.setup()`` fail the way a Django-less interpreter does."""

    def _boom() -> None:
        raise _NO_DJANGO

    monkeypatch.setattr(django_bootstrap, "_django_setup", _boom)


class TestFailureIsAudible:
    def test_failed_bootstrap_names_the_missing_capability(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _break_django_setup(monkeypatch)

        assert django_bootstrap.bootstrap_teatree_django() is False

        err = capsys.readouterr().err
        assert "cannot import Django" in err
        assert "No module named 'django'" in err

    def test_the_warning_is_emitted_once_per_process(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _break_django_setup(monkeypatch)

        django_bootstrap.bootstrap_teatree_django()
        django_bootstrap.bootstrap_teatree_django()

        assert capsys.readouterr().err.count("cannot import Django") == 1


class TestSuccessIsSilent:
    def test_successful_bootstrap_says_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert django_bootstrap.bootstrap_teatree_django() is True
        assert capsys.readouterr().err == ""
