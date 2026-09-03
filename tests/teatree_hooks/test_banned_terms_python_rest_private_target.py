"""A python REST post to a PROVABLY-internal project is not a public leak (#1415).

``gate_skips_for_visibility`` skips a post whose target repo is provably
non-public, and :func:`_python_rest_detection.find_python_forge_rest_urls`
already resolves the ``api/v<N>/projects/<slug>`` (GitLab) /
``repos/<owner>/<repo>`` (GitHub) target out of a python script's URL literal.
The two were never joined for the HEREDOC form: a heredoc body is not tokenised
into the segment's words, so ``_destination_from_python_script`` saw only
``['python3', '-', '<<PY']``, resolved no destination, and the fail-closed arm
scanned a post that could never reach a public surface.

These tests pin the fix and the invariants that keep it from widening the gate:

- a heredoc REST post to an allowlisted-private project is ALLOWED;
- the ``python3 -c`` form is ALLOWED the same way;
- a post to a NON-allowlisted project still BLOCKS (fail-closed on unknown);
- a script naming a private AND a public project still BLOCKS (every resolved
    target must be non-public, not merely the first one);
- a script that shells out to ``gh``/``glab`` still BLOCKS (the hidden-public-post
    vector a URL scan cannot see); and
- a high-confidence secret still BLOCKS on a private target.

Synthetic terms/namespaces only.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from hooks.scripts.hook_router import handle_banned_terms_pretool
from teatree.hooks import _repo_visibility

_TERM = "democorp"
_PRIVATE_NS = "democorp-engineering"
_PRIVATE_SLUG = f"{_PRIVATE_NS}/tracker"
_PUBLIC_SLUG = "openworld/tracker"
# Assembled at runtime: a credential-shaped literal in source trips every scanner that reads the file.
_SECRET_SHAPE = "glpat-" + "A" * 20


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, private_repos: list[str]) -> None:
    """Seed a DB-home config with the banned term + private allowlist, and isolate state."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        for key, value in {"banned_terms": [_TERM], "private_repos": private_repos}.items():
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("T3_CONFIG_DB", str(db))


def _bash(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _notes_url(slug: str) -> str:
    return f"https://gitlab.com/api/v4/projects/{slug.replace('/', '%2F')}/merge_requests/1/notes"


def _heredoc(body: str) -> str:
    return f"python3 - <<'PY'\n{body}\nPY"


def _post(url_var: str, note: str) -> str:
    """A real urllib write — without one the segment is not a REST publish at all."""
    return (
        "import urllib.request, json\n"
        f'data = json.dumps({{"body": "{note}"}}).encode()\n'
        f'urllib.request.urlopen(urllib.request.Request({url_var}, data=data, method="POST"))'
    )


class TestPythonRestPostToPrivateTarget:
    """A python REST post whose every resolved target is provably internal is allowed."""

    def test_heredoc_post_to_allowlisted_private_project_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(tmp_path, monkeypatch, private_repos=[_PRIVATE_NS])
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        cmd = _heredoc(f'url = "{_notes_url(_PRIVATE_SLUG)}"\n' + _post("url", f"status for {_TERM}"))
        assert handle_banned_terms_pretool(_bash(cmd)) is False, (
            "a REST post to a provably-internal project has no public-leak surface and must not hard-block"
        )
        assert capsys.readouterr().out == ""

    def test_dash_c_post_to_allowlisted_private_project_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(tmp_path, monkeypatch, private_repos=[_PRIVATE_NS])
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        script = f'import urllib.request; urllib.request.urlopen("{_notes_url(_PRIVATE_SLUG)}", b"{_TERM}")'
        assert handle_banned_terms_pretool(_bash(f'python3 -c "{script}"')) is False
        assert capsys.readouterr().out == ""


class TestPythonRestSkipStaysFailClosed:
    """The load-bearing half: the skip must not reach a target it cannot prove internal."""

    def test_post_to_non_allowlisted_project_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(tmp_path, monkeypatch, private_repos=[_PRIVATE_NS])
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        cmd = _heredoc(f'url = "{_notes_url(_PUBLIC_SLUG)}"\n' + _post("url", f"status for {_TERM}"))
        assert handle_banned_terms_pretool(_bash(cmd)) is True, "an unproven target must keep the fail-closed scan"
        assert "democorp" in capsys.readouterr().out

    def test_private_and_public_target_in_one_script_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(tmp_path, monkeypatch, private_repos=[_PRIVATE_NS])
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        cmd = _heredoc(
            f'url = "{_notes_url(_PRIVATE_SLUG)}"\n'
            f'mirror = "{_notes_url(_PUBLIC_SLUG)}"\n' + _post("url", f"status for {_TERM}")
        )
        assert handle_banned_terms_pretool(_bash(cmd)) is True, (
            "EVERY resolved target must be non-public — matching only the first one fails open"
        )
        assert capsys.readouterr().out != ""

    def test_script_shelling_out_to_glab_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(tmp_path, monkeypatch, private_repos=[_PRIVATE_NS])
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        cmd = _heredoc(
            "import subprocess\n"
            f'url = "{_notes_url(_PRIVATE_SLUG)}"\n'
            + _post("url", f"status for {_TERM}")
            + f'\nsubprocess.run(["glab", "mr", "create", "--description", "{_TERM}"])'
        )
        assert handle_banned_terms_pretool(_bash(cmd)) is True, (
            "a URL scan cannot see a forge CLI the script shells out to — that target stays unproven"
        )
        assert capsys.readouterr().out != ""

    def test_secret_in_private_target_body_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(tmp_path, monkeypatch, private_repos=[_PRIVATE_NS])
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        cmd = _heredoc(f'url = "{_notes_url(_PRIVATE_SLUG)}"\n' + _post("url", f"token {_SECRET_SHAPE}"))
        assert handle_banned_terms_pretool(_bash(cmd)) is True, (
            "secrets are blocked on every surface, private targets included"
        )
        assert capsys.readouterr().out != ""
