"""Integration tests for ``scripts/privacy_scan.py`` as a subprocess.

``t3 tool privacy-scan`` runs this script via
``ToolRunner.run_script`` → ``[sys.executable, script, *args]``. Without
an ``if __name__ == "__main__"`` guard the typer ``app`` is never
invoked and the script is a silent no-op (exit 0 on a planted secret),
which makes the retro/contribute privacy scan worthless. These tests
invoke the script the same way ``run_script`` does so the entrypoint is
exercised, not mocked.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.privacy_scan import PRIVACY_FINDINGS_EXIT_CODE

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "privacy_scan.py"


def _run(stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "-"],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_env(stdin: str, env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Invoke the script with a hermetic env: real banned-terms sources cleared first.

    Clearing the inherited ``T3_BANNED_TERMS`` env and ``T3_CONFIG_DB`` keeps a
    developer's real DB / env out of the assertion, so the test exercises only
    the seeded DB / env it sets.
    """
    env = {k: v for k, v in os.environ.items() if k not in {"T3_BANNED_TERMS", "T3_CONFIG_DB"}}
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "-"],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


class TestPrivacyScanScriptEntrypoint:
    def test_planted_api_key_exits_findings_code(self) -> None:
        result = _run("token = glpat-XXXXXXXXXXXXXXXX\n")  # privacy-scan:allow self-fixture
        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr

    def test_internal_home_path_exits_findings_code(self) -> None:
        result = _run("see /Users/someone/secret/path\n")  # privacy-scan:allow self-fixture
        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr

    def test_clean_text_exits_zero(self) -> None:
        result = _run("a perfectly ordinary line of prose\n")
        assert result.returncode == 0, result.stdout + result.stderr


class TestPrivacyScanOpaqueId:
    """Fix #3: the publish surface also flags real-shaped Slack/forge IDs.

    A channel/DM/user/app/team id (``C0…``/``D0…``/``U0…``/``A0…``/``T0…``)
    pushed to a public surface is a leak with no dictionary word, so the
    banned-term pass never caught it. The synthetic-placeholder allowlist
    keeps fixtures/examples from tripping.
    """

    def test_real_shaped_slack_id_is_a_finding(self) -> None:
        # Invented random-looking id — not a real channel id.
        result = _run("channel = C0ZX91QWERT\n")
        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr
        assert "opaque_id" in result.stdout
        assert "C0ZX91QWERT" in result.stdout

    def test_synthetic_placeholder_id_is_clean(self) -> None:
        result = _run("channel = C0DEMOCHAN1 and user U01ABCD1234\n")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_id_in_slack_archive_url_is_a_finding(self) -> None:
        result = _run("https://slack.com/archives/D0KP47MNBVC/p1717603200123456\n")
        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr
        assert "D0KP47MNBVC" in result.stdout


class TestPrivacyScanDedicatedFindingsExitCode:
    """A genuine finding exits on a dedicated code distinct from any crash (#126 gap 3).

    The pre-push leak gate previously treated ANY non-zero scan exit as a
    finding and BLOCKED — so a scanner crash, a missing script, or an
    argparse usage error (all non-zero, none of them a real finding) wedged
    every push closed. Reserving a dedicated ``PRIVACY_FINDINGS_EXIT_CODE``
    for "findings present" lets the hook block on THAT code only and fail
    open on every other non-zero.
    """

    def test_findings_exit_code_is_distinct_from_generic_failure_codes(self) -> None:
        """The findings code must not collide with the generic exception (1) or usage (2) codes."""
        assert PRIVACY_FINDINGS_EXIT_CODE not in {0, 1, 2}

    def test_genuine_finding_uses_the_dedicated_code(self) -> None:
        result = _run("token = glpat-XXXXXXXXXXXXXXXX\n")  # privacy-scan:allow self-fixture
        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr

    def test_argparse_usage_error_is_not_the_findings_code(self) -> None:
        """A bad flag (typer usage error) must exit on a code the hook reads as 'crash, allow'."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "-", "--no-such-flag"],
            input="clean\n",
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0
        assert proc.returncode != PRIVACY_FINDINGS_EXIT_CODE, proc.stdout + proc.stderr

    def test_missing_input_file_is_not_the_findings_code(self) -> None:
        """A missing input file (crash) must NOT masquerade as a finding."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "/no/such/file/exists.txt"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0
        assert proc.returncode != PRIVACY_FINDINGS_EXIT_CODE, proc.stdout + proc.stderr


class TestPrivacyScanCallerVisibleSummary:
    """Findings must reach a piped/non-TTY caller without a manual rerun (#696).

    The historical bug: findings were rendered only via a ``rich`` table on
    ``Console(stderr=True)``, which is invisible to scripted callers (and is
    captured-and-discarded by ``ToolRunner.run_script``). The scanner now
    always writes a deterministic plain-text summary to **stdout** so a
    piped caller reliably sees the offending line, category, and redacted
    match. ``capture_output=True`` below is exactly how ``run_script`` and
    the pre-push gate consume it — no TTY, no mocking of the scanner.
    """

    def test_planted_secret_summary_is_on_stdout_for_piped_caller(self) -> None:
        result = _run("token = glpat-XXXXXXXXXXXXXXXX\n")  # privacy-scan:allow self-fixture
        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr
        # The caller (run_script / the gate) reads stdout — the finding
        # detail must be there, not only in a stderr rich table.
        assert "api_key" in result.stdout
        assert "1" in result.stdout  # the offending line number
        assert "glpat-" in result.stdout  # redacted match prefix

    def test_internal_path_category_and_line_visible_on_stdout(self) -> None:
        result = _run("ok\nsee /Users/someone/secret/path\n")  # privacy-scan:allow self-fixture
        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr
        assert "home_path" in result.stdout
        assert "2" in result.stdout  # finding is on the second line

    def test_clean_input_prints_clear_clean_line_on_stdout(self) -> None:
        result = _run("a perfectly ordinary line of prose\n")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "clean" in result.stdout.lower()

    def test_json_output_still_valid(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "-", "--json"],
            input="token = glpat-XXXXXXXXXXXXXXXX\n",  # privacy-scan:allow self-fixture
            capture_output=True,
            text=True,
            check=False,
        )
        import json  # noqa: PLC0415

        parsed = json.loads(proc.stdout)
        assert isinstance(parsed, list)
        assert parsed[0]["category"] == "api_key"
        assert parsed[0]["line"] == 1

    def test_no_strict_warns_but_exits_zero_with_visible_summary(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "-", "--no-strict"],
            input="token = glpat-XXXXXXXXXXXXXXXX\n",  # privacy-scan:allow self-fixture
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "api_key" in proc.stdout


class TestPrivacyScanAllowAnnotation:
    """A line carrying the inline ``privacy-scan:allow`` annotation is exempt.

    Same idiom as gitleaks' ``gitleaks:allow``. Used so a repo's own
    privacy-scanner fixtures and the gate's own documentation examples do
    not self-block the gate, while a real leak on any line *without* the
    annotation is still caught.
    """

    def test_annotated_line_is_exempt(self) -> None:
        result = _run("token = glpat-XXXXXXXXXXXXXXXX  # privacy-scan:allow planted fixture\n")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_annotated_line_does_not_exempt_other_lines(self) -> None:
        text = (
            "token = glpat-XXXXXXXXXXXXXXXX  # privacy-scan:allow fixture\n"
            "real = glpat-YYYYYYYYYYYYYYYY\n"  # privacy-scan:allow self-fixture
        )
        result = _run(text)
        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr

    def test_annotation_only_exempts_its_own_line_not_a_substring_match(self) -> None:
        # The annotation must be the literal marker, not any line that
        # merely mentions the word "allow".
        result = _run("token = glpat-XXXXXXXXXXXXXXXX  # allow this please\n")  # privacy-scan:allow self-fixture
        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr


class TestDecoratorIsNotAnEmail:
    """Python decorators / attribute access must not be flagged as emails (#701).

    ``_EMAIL_RE`` historically matched ``<chars>@<domain>.<tld>`` loosely,
    so a diff line ``+@pytest.fixture`` (diff ``+`` as a fake local part)
    or ``@module.attr`` tripped the public-repo privacy gate. The fix
    tightens the local part so a real address is required while the
    decorator class of false positives is dropped — without weakening
    detection of genuine emails (which the ``privacy-scan:allow``
    convention still exempts when they are intentional fixtures).
    """

    @pytest.mark.parametrize(
        "line",
        [
            "+@pytest.fixture",
            "    @pytest.fixture",
            "@pytest.fixture",
            "@app.route",
            "+@app.route('/x')",
            "@dataclass",
            "@staticmethod",
            "@property",
            "@module.attr",
            "+    @some.decorator.chain",
        ],
    )
    def test_decorator_token_is_not_flagged(self, line: str) -> None:
        result = _run(line + "\n")
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize(
        "line",
        [
            "+contact me at someone@gmail.com please",  # privacy-scan:allow (dummy example address, test input)
            "real address: t@e.st in this diff",  # privacy-scan:allow (dummy example address, test input)
            "+    leak = 'someone@gmail.com'",  # privacy-scan:allow (dummy example address, test input)
        ],
    )
    def test_real_email_still_caught(self, line: str) -> None:
        result = _run(line + "\n")
        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr
        assert "email" in result.stdout

    def test_real_secret_on_same_line_as_decorator_still_flagged(self) -> None:
        # A decorator that is *not* an email must not mask a genuine
        # secret sharing the line.
        result = _run("@pytest.fixture  # token = glpat-XXXXXXXXXXXXXXXX\n")  # privacy-scan:allow self-fixture
        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr
        assert "api_key" in result.stdout

    def test_real_email_on_same_line_as_decorator_still_flagged(self) -> None:
        line = "@app.route  # owner someone@gmail.com"  # privacy-scan:allow (dummy example address, test input)
        result = _run(line + "\n")
        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr
        assert "email" in result.stdout


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


class TestGitSshRemoteIsNotAnEmail:
    """`git@host:org/repo.git` SSH remote URLs are not emails (#119 follow-up).

    The email regex matched the ``git@<host>`` transport prefix of an SSH
    git remote, so any test or code carrying a normal SSH remote URL
    (``git@<host>:<org>/<repo>.git``) tripped the public-repo privacy
    gate — a recurring false positive on perfectly benign, public,
    non-PII git syntax. An SSH-remote ``git@`` is followed by
    ``host:path``; a real email never has a ``:path`` after the domain.
    """

    @pytest.mark.parametrize(
        "line",
        [
            'assert _slug("git@github.com:souliane/teatree.git") == "souliane/teatree"',
            "git@gitlab.com:acme/team/backend.git",
            "+    remote = 'git@github.com:o/r.git'",
            "git@bitbucket.org:team/repo.git",
            "  url = git@github.com-host-alias:souliane/teatree.git",
        ],
    )
    def test_ssh_remote_not_flagged(self, line: str) -> None:
        result = _run(line + "\n")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_real_email_next_to_ssh_remote_still_caught(self) -> None:
        # Suppressing the SSH-remote prefix must not mask a genuine email
        # elsewhere on the same line.
        line = "git@github.com:o/r.git  # owner someone@gmail.com"  # privacy-scan:allow self-fixture
        result = _run(line + "\n")
        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr
        assert "email" in result.stdout


class TestPrivacyScanBannedTermsSource:
    """The banned-terms source is DB-home ``banned_terms``.

    The public-leak pre-push gate reads the SAME ``banned_terms`` list the
    commit/posting gates do: ``T3_BANNED_TERMS`` env override → the
    ``banned_terms`` ``ConfigSetting`` row → fail-closed (never a SILENT empty
    ban list). All terms are SYNTHETIC, so this public test leaks nothing.
    """

    def _seed(self, tmp_path: Path, terms: list[str]) -> Path:
        db = tmp_path / "config.sqlite3"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_terms', ?)",
            (json.dumps(terms),),
        )
        conn.commit()
        conn.close()
        return db

    def test_db_configured_term_is_a_finding(self, tmp_path: Path) -> None:
        # A configured brand term in the diff trips the pre-push leak gate.
        db = self._seed(tmp_path, ["acmeterm"])
        result = _run_env("a line mentioning acmeterm here\n", {"T3_CONFIG_DB": str(db)})
        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr
        assert "banned_term" in result.stdout
        assert "acmeterm" in result.stdout

    def test_db_configured_term_absent_from_input_is_clean(self, tmp_path: Path) -> None:
        db = self._seed(tmp_path, ["acmeterm"])
        result = _run_env("a perfectly ordinary line of prose\n", {"T3_CONFIG_DB": str(db)})
        assert result.returncode == 0, result.stdout + result.stderr

    def test_env_var_overrides_the_db_source(self, tmp_path: Path) -> None:
        db = self._seed(tmp_path, ["fromdb"])
        result = _run_env(
            "a line mentioning envterm here\n",
            {"T3_CONFIG_DB": str(db), "T3_BANNED_TERMS": "envterm"},
        )
        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr
        assert "envterm" in result.stdout

    def test_unset_row_warns_loudly_and_never_silently_inert(self, tmp_path: Path) -> None:
        # Anti-vacuity: a DB with no banned_terms row is a load-bug-shaped UNSET,
        # so the banned-terms detector must SAY it is inert on stderr rather than
        # silently degrade to an empty ban list. (The other detectors still run,
        # so the pre-push gate is never wedged.)
        empty_db = tmp_path / "empty.sqlite3"
        conn = sqlite3.connect(str(empty_db))
        conn.execute("CREATE TABLE teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT)")
        conn.commit()
        conn.close()
        result = _run_env("a perfectly ordinary line of prose\n", {"T3_CONFIG_DB": str(empty_db)})
        assert result.returncode == 0, result.stdout + result.stderr
        assert "banned-terms" in result.stderr.lower()
        assert "inert" in result.stderr.lower()

    def test_explicit_empty_list_is_a_silent_deliberate_no_op(self, tmp_path: Path) -> None:
        db = self._seed(tmp_path, [])
        result = _run_env("a perfectly ordinary line of prose\n", {"T3_CONFIG_DB": str(db)})
        assert result.returncode == 0, result.stdout + result.stderr
        assert "inert" not in result.stderr.lower()
