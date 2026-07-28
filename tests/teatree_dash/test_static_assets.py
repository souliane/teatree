"""The dashboard's static assets are tracked in git and served with DEBUG off (#3164).

Two blockers this guards against:

BLOCKING #1 — the vendored htmx/mermaid JS was excluded by the ``*.min.js``
``.gitignore`` rule, so a fresh checkout 404s every ``{% static %}`` JS load.

BLOCKING #2 — under gunicorn with ``DEBUG`` off Django's staticfiles app serves
nothing, so ``/static/`` 404s wholesale without WhiteNoise.
"""

import os
import re
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
#: The ONE brand mark. Both the site-root ``/favicon.ico`` route and the dash base
#: template's ``<link rel="icon">`` load this file, so the mark has a single home
#: rather than a copy inlined as a data URI in the template.
_FAVICON = "src/teatree/dash/static/dash/favicon.svg"
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
    # ALLOWED_HOSTS matters here and nowhere above: WhiteNoise short-circuits a
    # /static/ path before CommonMiddleware ever calls get_host(), while the
    # site-root /favicon.ico route goes through the full middleware stack.
    with tempfile.TemporaryDirectory() as static_root, override_settings(
        STATIC_ROOT=static_root, ALLOWED_HOSTS=["testserver"]
    ):
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
            "/static/dash/favicon.svg",
        ):
            status = client.get(path).status_code
            assert status == 200, f"{path} -> {status} under DEBUG off"
        # A browser asks for /favicon.ico unprompted on any page that declares no
        # icon link — Django's admin declares none, so this 404'd and the console
        # guard's response listener recorded it as an error.
        status = client.get("/favicon.ico", follow=True).status_code
        assert status == 200, f"/favicon.ico -> {status} under DEBUG off"
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


def test_favicon_has_exactly_one_source() -> None:
    """The dash template loads the same file the ``/favicon.ico`` route serves.

    The mark used to be inlined as a data URI in ``base.html`` while the site root
    served nothing, so the dash pages were clean and every other page (the admin
    index) 404'd. One tracked file, two consumers, no second copy to drift.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "--", _FAVICON],  # noqa: S607 — git on PATH, repo convention
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert _FAVICON in tracked, f"{_FAVICON} is not tracked — /favicon.ico 404s in a fresh checkout"

    base_template = (_REPO_ROOT / "src/teatree/dash/templates/dash/base.html").read_text(encoding="utf-8")
    assert "{% static 'dash/favicon.svg' %}" in base_template
    assert "data:image/svg+xml" not in base_template, "the inlined copy is the drift this replaces"

    urlconf = (_REPO_ROOT / "src/teatree/urls.py").read_text(encoding="utf-8")
    assert "dash/favicon.svg" in urlconf, "the site-root route must serve the same mark"


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


# The settings page's shipped-default verdict colours (#3775 P8 dashboard half). Colour is
# never the only signal there — each verdict carries a text icon and its own words — but the
# colour must still be legible, so the two tokens it reuses are computed against every
# surface in both themes rather than assumed.
_VERDICT_FOREGROUNDS = ("--ok", "--warn")
_SURFACES = ("--bg", "--surface", "--surface-2")
_BODY_TEXT_MINIMUM = 4.5


def _relative_luminance(hex_colour: str) -> float:
    channels = (int(hex_colour.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4))
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    first, second = _relative_luminance(foreground), _relative_luminance(background)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def test_settings_default_verdict_colours_meet_the_body_text_ratio() -> None:
    css = (_REPO_ROOT / "src/teatree/dash/static/dash/css/tokens.css").read_text(encoding="utf-8")
    palettes = [
        dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})", body))
        for _selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    ]
    complete = [tokens for tokens in palettes if set(_VERDICT_FOREGROUNDS + _SURFACES) <= set(tokens)]
    assert complete, "no palette block in tokens.css carries the verdict tokens"

    failures = [
        f"{fg} on {bg} = {_contrast(tokens[fg], tokens[bg]):.2f}"
        for tokens in complete
        for fg in _VERDICT_FOREGROUNDS
        for bg in _SURFACES
        if _contrast(tokens[fg], tokens[bg]) < _BODY_TEXT_MINIMUM
    ]
    assert not failures, "the settings default-verdict colours miss 4.5:1:\n" + "\n".join(failures)


#: Body text, as distinct from the verdict colours above — ``tokens.css`` claimed 4.5:1 for
#: every pair in a comment nobody could check, and light-mode ``--ink-muted`` on
#: ``--surface-2`` was 4.42:1, so the claim was wrong rather than the palette being right.
_BODY_FOREGROUNDS = ("--ink", "--ink-muted", "--brand")


def _theme_blocks(css: str) -> dict[str, dict[str, str]]:
    """Each ``{ … }`` block's token map, keyed by the selector text before it."""
    blocks: dict[str, dict[str, str]] = {}
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        tokens = dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})", body))
        if tokens:
            blocks[selector.strip()[-60:]] = tokens
    return blocks


def test_every_body_text_pair_meets_the_documented_ratio() -> None:
    css = (_REPO_ROOT / "src/teatree/dash/static/dash/css/tokens.css").read_text(encoding="utf-8")
    blocks = _theme_blocks(css)
    wanted = set(_BODY_FOREGROUNDS + _SURFACES)
    assert any(wanted <= set(tokens) for tokens in blocks.values()), "no complete palette block in tokens.css"

    failures = [
        f"{selector}: {fg} on {bg} = {_contrast(tokens[fg], tokens[bg]):.2f}"
        for selector, tokens in blocks.items()
        if wanted <= set(tokens)
        for fg in _BODY_FOREGROUNDS
        for bg in _SURFACES
        if _contrast(tokens[fg], tokens[bg]) < _BODY_TEXT_MINIMUM
    ]
    assert not failures, "tokens.css claims 4.5:1 for body text; these pairs do not:\n" + "\n".join(failures)
