"""``ensure_django`` — the single sanctioned ``django.setup()`` bootstrap.

The helper consolidates the 30+ inline ``import django`` +
``DJANGO_SETTINGS_MODULE`` setdefault + ``django.setup()`` blocks that had
drifted across the CLI under two private wrapper names. The call-site
authorization itself is pinned by the ``django-setup-bootstrap`` chokepoint
(``tests/quality/test_chokepoints.py``); here we pin the helper's own
contract: it sets the settings module default, is safe to call repeatedly,
and — souliane/teatree#4207 — never lets Django's reentrancy guard speak for a
registry an earlier failed bootstrap left stalled.

The #4207 cases drive a throwaway :class:`~django.apps.registry.Apps` rather
than the session's own registry: the states under test are "mid-populate" and
"stalled", and inflicting either on the live registry would take the whole test
process down with it. Everything else is real — ``Apps.populate`` runs unmocked,
so the reentrancy guard exercised is Django's own.
"""

import os
from unittest.mock import patch

import pytest
from django.apps import AppConfig
from django.apps.registry import Apps

import teatree.utils
from teatree.utils.django_bootstrap import DjangoBootstrapStalledError, _Bootstrap, ensure_django

_MISSING_APP = "teatree_no_such_app_4207"


@pytest.fixture(autouse=True)
def _forget_recorded_failure():
    """The recorded first failure is process-wide, so it must not leak between tests."""
    with patch.object(_Bootstrap, "first_failure", None):
        yield


def _unpopulated_registry() -> Apps:
    """A registry rewound to its pre-``populate()`` state.

    ``Apps(installed_apps=None)`` is refused while a global registry exists, so
    building a populated one and rewinding its flags is the only route.
    """
    registry = Apps(installed_apps=())
    registry.app_configs.clear()
    registry.all_models.clear()
    registry.apps_ready = registry.models_ready = registry.ready = False
    registry.loading = False
    registry.ready_event.clear()
    return registry


class _ReentrantProbeConfig(AppConfig):
    """An app whose ``ready()`` re-enters the bootstrap, as a nested dispatch does."""

    label = "reentrant_probe_4207"

    def ready(self) -> None:
        ensure_django()


class TestEnsureDjango:
    def test_sets_settings_module_default_and_calls_setup(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("django.setup") as setup,
        ):
            os.environ.pop("DJANGO_SETTINGS_MODULE", None)
            ensure_django()
            assert os.environ["DJANGO_SETTINGS_MODULE"] == "teatree.settings"
            setup.assert_called_once_with()

    def test_preserves_an_explicit_settings_module(self) -> None:
        with (
            patch.dict(os.environ, {"DJANGO_SETTINGS_MODULE": "overlay.settings"}, clear=False),
            patch("django.setup"),
        ):
            ensure_django()
            assert os.environ["DJANGO_SETTINGS_MODULE"] == "overlay.settings"

    def test_idempotent_across_repeated_calls(self) -> None:
        with patch("django.setup") as setup:
            ensure_django()
            ensure_django()
            assert setup.call_count == 2


class TestBootstrapReachedFromInsidePopulate:
    def test_defers_to_the_populate_frame_already_on_this_stack(self) -> None:
        registry = _unpopulated_registry()
        probe = _ReentrantProbeConfig("teatree.utils", teatree.utils)

        with (
            patch("django.apps.apps", registry),
            patch("django.setup", lambda: registry.populate([probe])),
        ):
            registry.populate([probe])

        assert registry.ready

    def test_still_bootstraps_when_no_populate_frame_is_live(self) -> None:
        with patch("django.setup") as setup:
            ensure_django()
            setup.assert_called_once_with()


class TestBootstrapAfterAStalledSetup:
    """#4207: a ``django.setup()`` that dies partway leaves ``loading`` stuck True.

    ``Apps.populate`` sets that flag under no ``try``/``finally``, and teatree has
    fail-safe call sites (``cli/config_view.py``, ``cli/push_gate_tools.py``,
    ``cli/ci.py``, ``cli/agent.py``) that swallow the first failure — so the next
    bootstrap in the process met Django's reentrancy guard and reported
    ``populate() isn't reentrant``, with the real cause gone.
    """

    @staticmethod
    def _stall(registry: Apps) -> ModuleNotFoundError:
        """Run a real, failing ``populate()`` and return the failure it stalled on."""
        with (
            patch("django.apps.apps", registry),
            patch("django.setup", lambda: registry.populate([_MISSING_APP])),
            pytest.raises(ModuleNotFoundError) as caught,
        ):
            ensure_django()
        assert registry.loading
        assert not registry.ready
        return caught.value

    def test_the_first_failure_propagates_unchanged(self) -> None:
        assert _MISSING_APP in str(self._stall(_unpopulated_registry()))

    def test_a_later_bootstrap_reports_the_stall_not_the_reentrancy_guard(self) -> None:
        registry = _unpopulated_registry()
        self._stall(registry)

        with (
            patch("django.apps.apps", registry),
            patch("django.setup", lambda: registry.populate([_MISSING_APP])),
            pytest.raises(RuntimeError) as caught,
        ):
            ensure_django()

        assert "isn't reentrant" not in str(caught.value)
        assert isinstance(caught.value, DjangoBootstrapStalledError)

    def test_a_later_bootstrap_chains_the_failure_that_caused_the_stall(self) -> None:
        registry = _unpopulated_registry()
        first_failure = self._stall(registry)

        with (
            patch("django.apps.apps", registry),
            patch("django.setup", lambda: registry.populate([_MISSING_APP])),
            pytest.raises(DjangoBootstrapStalledError) as caught,
        ):
            ensure_django()

        assert caught.value.__cause__ is first_failure

    def test_reports_an_unrecorded_stall_without_inventing_a_cause(self) -> None:
        registry = _unpopulated_registry()
        registry.loading = True

        with (
            patch("django.apps.apps", registry),
            patch("django.setup", lambda: registry.populate([_MISSING_APP])),
            pytest.raises(DjangoBootstrapStalledError) as caught,
        ):
            ensure_django()

        assert "not recorded" in str(caught.value)
        assert isinstance(caught.value.__cause__, RuntimeError)

    def test_chains_the_first_failure_not_the_last(self) -> None:
        registry = _unpopulated_registry()
        first = ValueError("settings are unreadable")
        later = TypeError("a second, later failure")

        def raise_(exc: BaseException) -> None:
            raise exc

        with patch("django.apps.apps", registry):
            for failure in (first, later):
                with (
                    patch("django.setup", lambda exc=failure: raise_(exc)),
                    pytest.raises(type(failure)),
                ):
                    ensure_django()

            registry.loading = True
            with (
                patch("django.setup", lambda: registry.populate([_MISSING_APP])),
                pytest.raises(DjangoBootstrapStalledError) as caught,
            ):
                ensure_django()

        assert caught.value.__cause__ is first

    def test_leaves_an_unrelated_failure_from_a_stalled_registry_unlabelled(self) -> None:
        registry = _unpopulated_registry()
        registry.loading = True

        def explode() -> None:
            message = "LOGGING_CONFIG is not importable"
            raise ValueError(message)

        with (
            patch("django.apps.apps", registry),
            patch("django.setup", explode),
            pytest.raises(ValueError, match="LOGGING_CONFIG"),
        ):
            ensure_django()
