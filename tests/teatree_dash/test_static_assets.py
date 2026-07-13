"""The dashboard's static assets are tracked in git and served with DEBUG off (#3164).

Two blockers this guards against:

BLOCKING #1 — the vendored htmx/mermaid JS was excluded by the ``*.min.js``
``.gitignore`` rule, so a fresh checkout 404s every ``{% static %}`` JS load.

BLOCKING #2 — under gunicorn with ``DEBUG`` off Django's staticfiles app serves
nothing, so ``/static/`` 404s wholesale without WhiteNoise.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VENDORED_JS = (
    "src/teatree/dash/static/dash/js/htmx-2.0.4.min.js",
    "src/teatree/dash/static/dash/js/mermaid-11.min.js",
    "src/teatree/dash/static/dash/js/idiomorph-ext-0.7.3.min.js",
)
# The vendored IBM Plex latin subsets (@font-face src in tokens.css). Same tracked +
# served contract as the JS: a fresh checkout must ship them or every glyph 404s.
_VENDORED_FONTS = (
    "src/teatree/dash/static/dash/fonts/ibm-plex-sans-400.woff2",
    "src/teatree/dash/static/dash/fonts/ibm-plex-sans-500.woff2",
    "src/teatree/dash/static/dash/fonts/ibm-plex-sans-600.woff2",
    "src/teatree/dash/static/dash/fonts/ibm-plex-mono-400.woff2",
    "src/teatree/dash/static/dash/fonts/ibm-plex-mono-500.woff2",
)

# The pytest suite runs under the minimal ``tests.django_settings`` (no staticfiles
# app, no WhiteNoise, no STATIC_ROOT), so it cannot exercise the production static
# config. This subprocess boots the REAL ``teatree.settings`` with DEBUG off — the
# exact deployed gunicorn condition — and proves WhiteNoise serves the collected tree.
_SERVE_UNDER_DEBUG_OFF = textwrap.dedent(
    """
    import tempfile
    import django
    django.setup()
    from django.conf import settings
    assert settings.DEBUG is False, "expected DEBUG off under T3_DEBUG=0"
    assert settings.MIDDLEWARE[1] == "whitenoise.middleware.WhiteNoiseMiddleware", settings.MIDDLEWARE
    from django.core.management import call_command
    from django.test import Client, override_settings
    with tempfile.TemporaryDirectory() as static_root, override_settings(STATIC_ROOT=static_root):
        call_command("collectstatic", interactive=False, verbosity=0)
        client = Client()
        for path in (
            "/static/dash/js/htmx-2.0.4.min.js",
            "/static/dash/js/mermaid-11.min.js",
            "/static/dash/js/idiomorph-ext-0.7.3.min.js",
            "/static/dash/css/dash.css",
            "/static/dash/css/tokens.css",
            "/static/dash/css/admin-theme.css",
            "/static/dash/fonts/ibm-plex-sans-400.woff2",
            "/static/dash/fonts/ibm-plex-mono-400.woff2",
        ):
            status = client.get(path).status_code
            assert status == 200, f"{path} -> {status} under DEBUG off"
    print("SERVED_OK")
    """
)


def test_vendored_js_is_not_gitignored() -> None:
    # A zero exit from `git check-ignore` means the path IS ignored — the blocker.
    for rel in _VENDORED_JS:
        result = subprocess.run(
            ["git", "check-ignore", rel],  # noqa: S607 — git on PATH, repo convention
            cwd=_REPO_ROOT,
            capture_output=True,
            check=False,
        )
        assert result.returncode != 0, f"{rel} is gitignored — templates load it via {{% static %}}"


def test_vendored_js_is_tracked_in_git() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "--", *_VENDORED_JS],  # noqa: S607 — git on PATH, repo convention
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    for rel in _VENDORED_JS:
        assert rel in tracked, f"{rel} is not tracked — it 404s in a fresh checkout"


def test_vendored_fonts_tracked_and_not_gitignored() -> None:
    for rel in _VENDORED_FONTS:
        ignored = subprocess.run(
            ["git", "check-ignore", rel],  # noqa: S607 — git on PATH, repo convention
            cwd=_REPO_ROOT,
            capture_output=True,
            check=False,
        )
        assert ignored.returncode != 0, f"{rel} is gitignored — tokens.css @font-face loads it"
    tracked = subprocess.run(
        ["git", "ls-files", "--", *_VENDORED_FONTS],  # noqa: S607 — git on PATH, repo convention
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    for rel in _VENDORED_FONTS:
        assert rel in tracked, f"{rel} is not tracked — a missing glyph file 404s in a fresh checkout"


@pytest.mark.integration
def test_static_is_served_with_debug_off() -> None:
    # Booted against the production ``teatree.settings`` with DEBUG off (the
    # deployed gunicorn condition) — Django's staticfiles app serves nothing there,
    # so a 200 proves WhiteNoise is doing the serving.
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "teatree.settings", "T3_DEBUG": "0"}
    result = subprocess.run(
        [sys.executable, "-c", _SERVE_UNDER_DEBUG_OFF],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"static-serving check failed:\n{result.stdout}\n{result.stderr}"
    assert "SERVED_OK" in result.stdout
