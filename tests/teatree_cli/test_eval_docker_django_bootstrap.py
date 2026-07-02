"""``run_eval_in_docker``'s metered lane bootstraps Django before touching the ORM.

:func:`~teatree.cli.eval.docker.run_eval_in_docker` is the single chokepoint
every metered/benchmark caller (``t3 eval run --backend api``, ``t3 eval
benchmark``, and the bare ``t3 eval`` full-suite lane) routes through before
Docker. Its ``_requests_api_lane`` branch lazily imports
:mod:`teatree.credential_config` to resolve ``ANTHROPIC_API_KEY`` — a
domain-layer module that transitively imports real Django model classes
(``AnthropicActivePick`` / ``AnthropicTokenUsage``). Defining a Django model
class before ``django.setup()`` has run raises
``django.core.exceptions.ImproperlyConfigured``, not the intended fail-loud
``CredentialError`` — a real bug on every host invocation that routes to
Docker before ``ensure_django()`` (souliane/teatree PR #2877 review HOLD).

``pytest-django`` bootstraps Django for the WHOLE test session before any test
body runs (``DJANGO_SETTINGS_MODULE`` is set via ``pyproject.toml``'s
``[tool.pytest.ini_options]``, and pytest-django's ``pytest_configure`` calls
``django.setup()`` eagerly). That means the exact "credential_config imported
before django.setup()" ordering this bug depends on can NEVER occur inside an
in-process ``TestCase``/``TransactionTestCase`` — Django is already configured
by the time any test method runs. This bug class is invisible to
``TestCase``-based tests by construction, which is exactly why the PR's
conversion of ``test_eval_docker.py`` to Django ``TestCase`` classes did not
catch it.

These tests use two genuinely fresh child interpreters instead (mirroring
``tests/teatree_cli/test_review_django_bootstrap.py``, the existing pattern in
this codebase for pinning this bug class): a PREP subprocess bootstraps Django
against an isolated, empty ``XDG_DATA_HOME`` sandbox and applies migrations
(so the later ORM query has a schema to read, without seeding real
credentials); a PROBE subprocess then imports ``teatree.cli.eval.docker`` with
NO Django bootstrap of its own and calls ``run_eval_in_docker`` directly for a
metered lane — proving credential resolution reaches a real, fail-loud
``CredentialError`` instead of crashing on ``ImproperlyConfigured``.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_TIMEOUT_S = 60


def _isolated_env(xdg_data_home: Path, password_store_dir: Path) -> dict[str, str]:
    """A clean child env: isolated DB sandbox, empty ``pass`` store, no stray creds.

    Mirrors ``_clean_env()`` in ``test_review_django_bootstrap.py`` — no
    ``DJANGO_SETTINGS_MODULE`` pre-exported, the way a normal shell invocation
    of ``t3`` would be. ``XDG_DATA_HOME`` points ``teatree.settings`` at a
    throwaway sqlite file (never the real canonical DB or this worktree's
    auto-isolated one — see ``teatree.paths.resolve_data_dir``).
    ``PASSWORD_STORE_DIR`` points ``pass`` at an empty, uninitialized store so
    ``read_pass`` deterministically finds nothing, and ``ANTHROPIC_API_KEY`` is
    stripped — together these guarantee credential resolution fails loud
    rather than depending on whatever real credentials happen to be configured
    on the machine running the test.
    """
    env = os.environ.copy()
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env.pop("ANTHROPIC_API_KEY", None)
    env["XDG_DATA_HOME"] = str(xdg_data_home)
    env["PASSWORD_STORE_DIR"] = str(password_store_dir)
    return env


def _migrate_isolated_db(env: dict[str, str]) -> None:
    """Bootstrap Django + apply migrations in a disposable child interpreter.

    A separate process from the probe: by the time the probe runs, this
    interpreter (and its ``django.setup()`` call) is long gone, so the probe
    still starts with Django genuinely unconfigured. Only the on-disk sqlite
    schema (created by this migrate) persists between the two.
    """
    prep = textwrap.dedent("""
        import django, os
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
        django.setup()
        from django.core.management import call_command
        call_command("migrate", "--no-input", verbosity=0)
        """)
    result = subprocess.run(
        [sys.executable, "-c", prep], env=env, capture_output=True, text=True, check=False, timeout=_TIMEOUT_S
    )
    assert result.returncode == 0, f"migrate prep failed:\nstdout={result.stdout!r}\nstderr={result.stderr!r}"


_PROBE = textwrap.dedent("""
    from unittest.mock import patch

    import teatree.cli.eval.docker as docker_mod
    from teatree.llm.credentials import CredentialError

    with (
        patch.object(docker_mod.shutil, "which", return_value="/usr/bin/docker"),
        patch.object(docker_mod, "_image_present", return_value=True),
        patch.object(docker_mod, "_run_in_image", return_value=0),
    ):
        try:
            code = docker_mod.run_eval_in_docker({eval_args!r})
        except CredentialError as exc:
            print("CREDENTIAL_ERROR:", exc)
        else:
            print("EXIT_CODE:", code)
    """)


@pytest.mark.parametrize(
    "eval_args",
    [
        pytest.param(["benchmark", "--models", "claude-haiku-4-5@low"], id="benchmark"),
        pytest.param(["run", "--backend", "api"], id="run_backend_api"),
    ],
)
def test_credential_resolution_survives_a_cold_process_before_django_is_configured(
    tmp_path: Path, eval_args: list[str]
) -> None:
    """A metered lane's Docker-routed credential resolution never crashes on unconfigured Django.

    Reproduces the exact bug: a fresh process (no pytest-django bootstrap,
    Django genuinely unconfigured) calls ``run_eval_in_docker`` for a metered
    lane. Pre-fix, the lazy ``from teatree.credential_config import
    resolve_api_key_credential`` import defines Django model classes before
    ``django.setup()`` and raises ``ImproperlyConfigured``. Post-fix,
    ``run_eval_in_docker`` calls ``ensure_django()`` first, so the same import
    succeeds and credential resolution runs for real — failing loud with
    ``CredentialError`` (the isolated sandbox has no configured accounts and
    no reachable ``pass`` entry) instead of crashing on a Django configuration
    error.
    """
    xdg = tmp_path / "xdg"
    password_store = tmp_path / "empty-password-store"
    password_store.mkdir()
    env = _isolated_env(xdg, password_store)

    _migrate_isolated_db(env)

    probe = _PROBE.format(eval_args=eval_args)
    result = subprocess.run(
        [sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=False, timeout=_TIMEOUT_S
    )

    assert "ImproperlyConfigured" not in result.stdout
    assert "ImproperlyConfigured" not in result.stderr
    assert "AppRegistryNotReady" not in result.stdout
    assert "AppRegistryNotReady" not in result.stderr
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert "CREDENTIAL_ERROR:" in result.stdout, (
        f"expected a fail-loud CredentialError (no accounts configured in the isolated sandbox); "
        f"got stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
