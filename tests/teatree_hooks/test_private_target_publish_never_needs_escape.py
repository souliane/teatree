"""A publish to a PROVABLY-PRIVATE repo never needs an escape flag (#1213/#1415/#2369).

The operator's standing config declares the internal namespace in BOTH
``internal_publish_namespaces`` and ``private_repos``. With that in place, an MR
create toward the internal namespace must pass BOTH pre-publish leak gates with
NO ``QUOTE_OK=1`` / ``ALLOW_BANNED_TERM=1`` -- including the multi-line-body
shapes ``glab mr create`` forces (no ``--description-file`` flag exists, so the
body rides a ``$(cat <file>)`` substitution, a ``$VAR``, or a heredoc) whose
body the cold hook cannot always read before the command runs. An unreadable
body toward a private target is NOT a leak: there is no public surface.

Three directions per gate (anti-vacuity -- the private row must be green because
the target is provably non-public, not because the gate stopped detecting):

- PROVABLY-PRIVATE target (namespace-configured, no probe needed): ALLOWED with
    no escape flag, for a readable body, an unreadable ``$(cat …)`` body, and an
    unavailable ``$VAR`` body alike.
- Affirmatively-PUBLIC target: the SAME shapes still BLOCK (fail closed).
- UNKNOWN target (probe unavailable, not configured): still BLOCKS (fail
    closed, #3442).

Synthetic namespaces only (``demo-engineering``); the real config never appears
in tests.
"""

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from hooks.scripts.hook_router import handle_banned_terms_pretool, handle_quote_scanner_pretool
from teatree.hooks import _repo_visibility

_NS = "demo-engineering"
_TERM = "democorp"

Handler = Callable[[dict], bool | None]

_GATES = [
    pytest.param(handle_banned_terms_pretool, id="banned-terms-#1415"),
    pytest.param(handle_quote_scanner_pretool, id="quote-scanner-#1213"),
]


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, internal: bool) -> None:
    """Seed the DB-home config; ``internal`` adds the operator's namespace rows."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("QUOTE_OK", raising=False)
    monkeypatch.delenv("ALLOW_BANNED_TERM", raising=False)
    rows: dict[str, object] = {"banned_terms": [_TERM]}
    if internal:
        rows["internal_publish_namespaces"] = [_NS]
        rows["private_repos"] = [_NS]
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        for key, value in rows.items():
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


def _pin_probe(monkeypatch: pytest.MonkeyPatch, verdict: str | None) -> None:
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: verdict)


def _mr_create(slug: str, description: str) -> str:
    return f"glab mr create -R {slug} --title t --description {description}"


class TestProvablyPrivateTargetNeverNeedsEscape:
    """The RED-before rows: every blocked shape from the field, now clean with no flag."""

    @pytest.mark.parametrize("handler", _GATES)
    def test_unreadable_cat_substitution_body_is_allowed(
        self, handler: Handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(tmp_path, monkeypatch, internal=True)
        _pin_probe(monkeypatch, None)  # namespace config alone proves the target; no probe
        cmd = _mr_create(f"{_NS}/product", f'"$(cat {tmp_path}/absent-body.md)"')
        assert handler(_bash(cmd)) is False, "an unreadable body toward a provably-private target must not block"
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize("handler", _GATES)
    def test_unavailable_var_body_is_allowed(
        self, handler: Handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(tmp_path, monkeypatch, internal=True)
        _pin_probe(monkeypatch, None)
        cmd = _mr_create(f"{_NS}/product", '"$MR_BODY"')
        assert handler(_bash(cmd)) is False
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize("handler", _GATES)
    def test_bodyfile_writer_chain_is_allowed(
        self, handler: Handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Write the body file, then post it -- one command, no escape flag.
        _seed(tmp_path, monkeypatch, internal=True)
        _pin_probe(monkeypatch, None)
        body_file = tmp_path / "mr-body.md"
        cmd = f"cat > {body_file} <<'EOF'\nrolling out {_TERM} support\nEOF\n" + _mr_create(
            f"{_NS}/product", f'"$(cat {body_file})"'
        )
        assert handler(_bash(cmd)) is False
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize("handler", _GATES)
    def test_field_shape_issue_create_with_stderr_redirect_is_allowed(
        self, handler: Handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The EXACT live-blocked shape (2026-07-23): a cd-prefixed issue create
        # to the internal namespace with a readable inline body carrying the
        # term, ending in the ubiquitous ``2>&1``. The lexer used to split the
        # ``&`` as a separator, making the trailing ``1`` a phantom segment
        # whose unknown leader vetoed the ALL-SEGMENTS private skip.
        _seed(tmp_path, monkeypatch, internal=True)
        _pin_probe(monkeypatch, None)
        cmd = (
            f"cd {tmp_path} && glab issue create --repo {_NS}/client-workspace \\\n"
            f'  --title "e2e: {_TERM} lane needs a new DEV account" \\\n'
            f'  --description "Test body mentioning {_TERM} to check gate classification." 2>&1'
        )
        assert handler(_bash(cmd)) is False, "the live-blocked 2>&1 shape must skip on a private target"
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize("handler", _GATES)
    def test_heredoc_substitution_body_with_stderr_redirect_is_allowed(
        self, handler: Handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The second live-blocked shape: the body fed via ``"$(cat <<'EOF' ...)"``
        # plus the trailing ``2>&1``.
        _seed(tmp_path, monkeypatch, internal=True)
        _pin_probe(monkeypatch, None)
        cmd = (
            f'glab issue create --repo {_NS}/client-workspace --title "t" '
            f"--description \"$(cat <<'EOF'\nThe {_TERM} lane has been failing.\nEOF\n)\" 2>&1"
        )
        assert handler(_bash(cmd)) is False
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize("handler", _GATES)
    def test_pager_suppressing_pipe_filter_is_allowed(
        self, handler: Handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ``| head``-style read-only filters cannot publish; they must not veto
        # the ALL-SEGMENTS private skip.
        _seed(tmp_path, monkeypatch, internal=True)
        _pin_probe(monkeypatch, None)
        cmd = _mr_create(f"{_NS}/product", f'"uses {_TERM}"') + " 2>&1 | head -20"
        assert handler(_bash(cmd)) is False
        assert capsys.readouterr().out == ""


class TestPublicTargetStillBlocks:
    """Anti-vacuity: the SAME shapes toward an affirmatively-PUBLIC target still block."""

    @pytest.mark.parametrize("handler", _GATES)
    def test_unreadable_cat_substitution_body_blocks(
        self, handler: Handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(tmp_path, monkeypatch, internal=False)
        _pin_probe(monkeypatch, "PUBLIC")
        cmd = _mr_create("someowner/public-svc", f'"$(cat {tmp_path}/absent-body.md)"')
        assert handler(_bash(cmd)) is True, "an unreadable body toward a PUBLIC target must stay fail-closed"
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    @pytest.mark.parametrize("handler", _GATES)
    def test_leaky_body_with_stderr_redirect_blocks(
        self, handler: Handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The ``2>&1``/pipe-filter tolerance must not weaken the PUBLIC scan.
        _seed(tmp_path, monkeypatch, internal=False)
        _pin_probe(monkeypatch, "PUBLIC")
        leak = f'"## User ask (verbatim)\nplease ship {_TERM} now"'
        cmd = f"glab mr create -R someowner/public-svc --title t --description {leak} 2>&1 | head -20"
        assert handler(_bash(cmd)) is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"


class TestUnknownTargetStaysFailClosed:
    """A target the gate cannot classify (probe unavailable, not configured) still blocks."""

    @pytest.mark.parametrize("handler", _GATES)
    def test_unreadable_cat_substitution_body_blocks(
        self, handler: Handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(tmp_path, monkeypatch, internal=False)
        _pin_probe(monkeypatch, None)
        cmd = _mr_create("someowner/mystery-svc", f'"$(cat {tmp_path}/absent-body.md)"')
        assert handler(_bash(cmd)) is True, "an unclassifiable target must stay fail-closed (#3442)"
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"
