import json
import os
import subprocess
import sys

_LOGIN_GATE = "teatree.core.middleware.OperatorLoginRequiredMiddleware"
_PROBE = (
    "import json; from teatree import settings as base, webhook_settings as listener; "
    "print(json.dumps({name: {key: getattr(module, key) for key in ('DEBUG', 'MIDDLEWARE', 'INSTALLED_APPS')} "
    "for name, module in (('base', base), ('listener', listener))}))"
)


def _settings(**env: str) -> dict[str, dict[str, object]]:
    probe_env = {key: value for key, value in os.environ.items() if key != "T3_DEBUG"} | env
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], check=False, capture_output=True, text=True, env=probe_env, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_listener_debug_off_without_env() -> None:
    loaded = _settings()

    assert loaded["base"]["DEBUG"] is True
    assert loaded["listener"]["DEBUG"] is False


def test_listener_debug_off_even_when_env_enables_it() -> None:
    assert _settings(T3_DEBUG="1")["listener"]["DEBUG"] is False


def test_listener_has_no_login_gate() -> None:
    loaded = _settings()

    assert _LOGIN_GATE in loaded["base"]["MIDDLEWARE"]
    assert not {
        _LOGIN_GATE,
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
    }.intersection(loaded["listener"]["MIDDLEWARE"])


def test_listener_installs_no_admin_dash_or_staticfiles() -> None:
    loaded = _settings()
    surfaces = {"django.contrib.admin", "django.contrib.staticfiles", "teatree.dash"}

    assert surfaces <= set(loaded["base"]["INSTALLED_APPS"])
    assert not surfaces.intersection(loaded["listener"]["INSTALLED_APPS"])
