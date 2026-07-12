"""Cold-host liveness for the banned-terms leak gate's own ``src/`` bootstrap (HLG-2).

Every OTHER leak-gate liveness / fail-closed test runs with ``teatree`` ALREADY
importable — the editable install makes it so — so none exercises the path that
actually broke: a COLD host where ``teatree`` is not importable until the gate's
own bootstrap puts the sibling ``src/`` on ``sys.path`` (#1314). An off-by-one in
that bootstrap (``parents[2] / "src"`` → the nonexistent ``hooks/src`` for a
package-subdir module) makes the ``from teatree.hooks import ...`` fail, the
handler's ``except`` fail-opens, and the banned-terms leak gate silently passes a
banned term onto a PUBLIC surface (HLG-1/HLG-5).

Reproducing the bug requires TWO conditions the existing tests never combine.

FIRST, ``teatree`` NOT importable at handler entry. Achieved with ``python -S``
(no site processing → the editable install's ``.pth``/finder is inert), so the
ONLY route to ``teatree`` is the gate's bootstrap — the true cold host.

SECOND, the banned-terms handler is the SOLE teatree bootstrapper. The full
router runs earlier PreToolUse handlers that bootstrap teatree correctly first;
once ``teatree`` is imported its ``__path__`` is set and every later submodule
import succeeds regardless of ``sys.path``, MASKING this gate's own off-by-one.
So the handler is driven in ISOLATION here — importing only
``hooks.scripts.banned_terms.gate`` and calling it directly — exactly as a run
where it happens to be the first (or only) gate to reach teatree.

Anti-vacuity: this test is RED against the pre-fix ``parents[2]`` bootstrap (the
cold-host banned term is silently PASSED, exit 0) and GREEN once the gate routes
through the shared ``managed_repo.teatree_src_on_path`` helper (blocked, exit 2).
Confirmed failing before the fix.

The final class also covers HLG-3: an INTERNAL error in the handler must stay
fail-open (never-lockout) but NOT silent — it emits a loud stderr NOTE so the
unscanned-body fail-open on the PUBLIC-egress path is diagnosable.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_BANNED_TERM = "acmecorp"
# The gate blocks a banned term only on an affirmatively-PUBLIC destination; the
# probe slug is the forge-qualified form of the ``--repo`` argument below.
_PROBE_SLUG = "github.com/souliane/teatree"

# Exit-code contract of the isolation driver below (mirrors the router ``main()``:
# 2 = deny, 0 = passthrough), plus two precondition sentinels.
_EXIT_DENY = 2
_EXIT_PASS = 0
_EXIT_TEATREE_ALREADY_IMPORTABLE = 3
_EXIT_GATE_IMPORT_PULLED_TEATREE = 4

# Drives the banned-terms handler in ISOLATION under ``python -S`` (cold host).
# It proves the cold-host precondition (teatree not importable, and importing the
# gate does not pull it in) BEFORE calling the handler, so a GREEN result can only
# come from the gate's OWN bootstrap resolving ``src/`` correctly.
_DRIVER = """
import json
import sys
from pathlib import Path

repo_root = Path(sys.argv[1])
sys.path.insert(0, str(repo_root))  # make hooks.scripts.* importable; NOT src/

try:
    import teatree  # noqa: F401 — probe import: its SUCCESS is the assertion, the module is unused
    sys.exit(3)  # precondition failed: teatree already importable (not a cold host)
except ImportError:
    pass

from hooks.scripts.banned_terms.gate import handle_banned_terms_pretool

if "teatree" in sys.modules:
    sys.exit(4)  # precondition failed: importing the gate pre-imported teatree

blocked = handle_banned_terms_pretool(json.loads(sys.stdin.read()))
sys.exit(2 if blocked else 0)
"""


def _seed_config_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_terms', ?)",
            (json.dumps([_BANNED_TERM]),),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_public_visibility(data_dir: Path) -> None:
    """Pin the destination PUBLIC via the day-cache so the gate never live-probes.

    ``_repo_visibility.slug_visibility`` reads this cache before probing, so a
    seeded PUBLIC verdict makes the banned-term deny deterministic in a subprocess
    with no authenticated ``gh``/``glab`` on PATH.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "repo-visibility-cache.json").write_text(
        json.dumps({_PROBE_SLUG: {"ts": time.time(), "visibility": "PUBLIC"}}),
        encoding="utf-8",
    )


def _public_post_with(payload: str) -> str:
    return f'gh issue create --repo souliane/teatree --title t --body "{payload}"'


class _ColdHost:
    """A seeded cold-host fixture: config DB + PUBLIC visibility cache + ``-S`` runner."""

    def __init__(self, tmp_path: Path) -> None:
        self._db = tmp_path / "config.sqlite3"
        self._data = tmp_path / "data"
        self._driver = tmp_path / "driver.py"
        _seed_config_db(self._db)
        _seed_public_visibility(self._data)
        self._driver.write_text(_DRIVER, encoding="utf-8")

    def run(self, command: str) -> subprocess.CompletedProcess[str]:
        env = {
            # A real PATH so the shell scanner's ``uv run`` resolves; HOME for uv's
            # cache. ``-S`` keeps site (and thus the editable install) out.
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
            "HOME": os.environ.get("HOME", str(self._data)),
            "T3_CONFIG_DB": str(self._db),
            "T3_DATA_DIR": str(self._data),
            "TEATREE_CLAUDE_STATUSLINE_STATE_DIR": str(self._data / "state"),
        }
        return subprocess.run(
            [sys.executable, "-S", str(self._driver), str(_REPO_ROOT)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )


@pytest.mark.integration
class TestBannedTermsGateColdHostBootstrap:
    """The gate STILL loads teatree via its bootstrap and scans on a cold host."""

    def test_cold_host_precondition_teatree_not_importable_under_dash_s(self) -> None:
        # Proves the reproduction is not vacuous: under ``-S`` teatree is genuinely
        # absent, so a GREEN block below can only come from the gate's bootstrap.
        proc = subprocess.run(
            [sys.executable, "-S", "-c", "import teatree"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0, "teatree must NOT be importable under -S (the cold host)"
        assert "No module named 'teatree'" in proc.stderr, proc.stderr

    def test_cold_host_banned_term_is_still_blocked(self, tmp_path: Path) -> None:
        result = _ColdHost(tmp_path).run(_public_post_with(f"rolling out {_BANNED_TERM} integration"))
        assert result.returncode != _EXIT_TEATREE_ALREADY_IMPORTABLE, (
            "cold-host precondition broke: teatree was importable before the bootstrap"
        )
        assert result.returncode != _EXIT_GATE_IMPORT_PULLED_TEATREE, (
            "cold-host precondition broke: importing the gate pre-imported teatree"
        )
        assert result.returncode == _EXIT_DENY, (
            "on a cold host the banned-terms gate must STILL bootstrap teatree and BLOCK the "
            f"banned term (exit {_EXIT_DENY}); an off-by-one bootstrap fail-opens (exit 0). "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_cold_host_benign_body_is_not_blocked(self, tmp_path: Path) -> None:
        result = _ColdHost(tmp_path).run(_public_post_with("just a normal update"))
        assert result.returncode == _EXIT_PASS, (
            "a benign public post must pass on a cold host too — the gate must not over-block. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )


class TestBannedTermsGateInternalErrorFailsOpenLoudly:
    """An internal error stays fail-open (never-lockout) but is LOUD, not silent (HLG-3)."""

    def test_internal_error_returns_false_and_notes_on_stderr(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hooks.scripts.banned_terms import gate  # noqa: PLC0415 — in-process handler under test

        error_message = "simulated internal gate error"

        def _boom(_data: dict) -> bool:
            raise RuntimeError(error_message)

        monkeypatch.setattr(gate, "_run_banned_terms_pretool", _boom)
        blocked = gate.handle_banned_terms_pretool(
            {"tool_name": "Bash", "tool_input": {"command": _public_post_with("anything")}}
        )
        captured = capsys.readouterr()
        # Fail OPEN: a crashing hook must never wedge the agent (never-lockout).
        assert blocked is False
        # But NOT silent: the fail-open on the public-egress path is named loudly so
        # it is diagnosable instead of an invisible no-op.
        assert "banned-terms publish gate failed open" in captured.err
        assert "RuntimeError: simulated internal gate error" in captured.err
        assert "NOT a clean scan" in captured.err
