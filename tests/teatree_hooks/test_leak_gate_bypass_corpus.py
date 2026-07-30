"""Adversarial corpus for the leak-gate canonicalization bypasses (F7.1-F7.8).

Each DENY row is a command shape that, before the unified canonicalization,
evaded the quote gate + banned-terms gate by making publish DETECTION and body
EXTRACTION disagree -- a wrapper/path leader (``sh -c``, ``xargs gh``,
``/usr/bin/gh``, ``env gh``), a lowercase env prefix (``foo=1 gh``), an unquoted
``$VAR`` body, a mid-body heredoc terminator, a ``curl -F`` multipart field, or a
variable merge-endpoint iid. Every disagreement now fails CLOSED.

The ALLOW rows (the over-block guard) are as load-bearing as the DENY rows: a
read-only ``grep``/``rg``/``cat`` that merely QUOTES a forge spelling, a clean
env body, a private-repo post, an ``ssh`` with no forge call, and a commit whose
message DISCUSSES the bypass must NOT block. A broad "any non-forge leader
carrying a forge token" rule would lock the user out of ordinary inspection.

SYNTHETIC namespaces / banned terms only (``acme-internal``, ``internalcorp``,
``acmecorp``, ``acmewidget``, public ``souliane/teatree``); the fixture config is
injected so the test never reads the real DB-home store, and a fake ``ghp_``
secret is never a real credential.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from teatree.hooks import _repo_visibility, banned_terms_scanner, public_visibility, publish_surface
from teatree.hooks._command_parser import extract_bash_payload, is_fail_closed_sentinel, is_publish_command

_FAKE_SECRET = "ghp_" + "A" * 40


def _seed_config_db(tmp_path: Path, **rows: object) -> Path:
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
    return db


@pytest.fixture
def config(tmp_path: Path) -> Path:
    return _seed_config_db(
        tmp_path,
        private_repos=["acme-internal", "internalcorp"],
        internal_publish_namespaces=["acme-internal", "internalcorp"],
        banned_terms=["acmecorp", "acmewidget"],
    )


@pytest.fixture(autouse=True)
def _public_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve ``souliane/teatree`` as PUBLIC; delegate every other slug to the real probe."""
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "viscache"))
    real_probe = _repo_visibility.probe_visibility
    monkeypatch.setattr(
        _repo_visibility,
        "probe_visibility",
        lambda slug: "PUBLIC" if "souliane/teatree" in slug else real_probe(slug),
    )


def _verdict(command: str, config_path: Path, cwd: Path | None = None) -> str:
    """Return ``"allow"``/``"block"`` mirroring ``hook_router._run_banned_terms_pretool``."""
    tool_input = {"command": command}
    if publish_surface.contains_secret(banned_terms_scanner.secret_scan_text("Bash", tool_input)):
        return "block"
    payload = banned_terms_scanner.extract_publish_payload("Bash", tool_input)
    if payload is None:
        return "allow"
    skipped = banned_terms_scanner.has_override("Bash", tool_input) or public_visibility.gate_skips_for_visibility(
        command, cwd, config_path=config_path
    )
    if skipped or banned_terms_scanner.scan_text(payload, config_path=config_path) is None:
        return "allow"
    if publish_surface.carve_out_applies("Bash", command, payload, cwd, config_path=config_path):
        return "allow"
    return "block"


# ── F7.1/F7.3/F7.7 wrapper, path, env-prefix, curl-form leak spellings ──

_PUBLIC = "-R souliane/teatree"

# (label, command). Every row carries the synthetic banned term ``acmecorp``
# toward the PUBLIC repo through a DIFFERENT canonicalization bypass.
_DENY_SPELLINGS: list[tuple[str, str]] = [
    ("interpreter sh -c", f'sh -c "gh pr create {_PUBLIC} --body acmecorp"'),
    ("interpreter bash -lc", f'bash -lc "gh pr create {_PUBLIC} --body acmecorp"'),
    ("interpreter eval", f'eval "gh pr create {_PUBLIC} --body acmecorp"'),
    ("interpreter ssh host gh", f"ssh host gh pr create {_PUBLIC} --body acmecorp"),
    ("wrapper xargs", f"xargs gh pr create {_PUBLIC} --body acmecorp"),
    ("wrapper env pager", f"env GH_PAGER= gh pr create {_PUBLIC} --body acmecorp"),
    ("wrapper command", f"command gh pr create {_PUBLIC} --body acmecorp"),
    ("wrapper nohup", f"nohup gh pr create {_PUBLIC} --body acmecorp"),
    ("path /usr/bin/gh", f"/usr/bin/gh pr create {_PUBLIC} --body acmecorp"),
    ("path ./gh", f"./gh pr create {_PUBLIC} --body acmecorp"),
    ("env-prefix lowercase api", "foo=1 gh api repos/souliane/teatree/issues -f body=acmecorp"),
    ("env-prefix mixed-case note", f"Foo_bar=1 glab mr note 7 {_PUBLIC} --message acmecorp"),
    ("unresolved gh pr review", f"gh pr review 5 {_PUBLIC} --body acmecorp"),
    ("curl -F multipart", 'curl -F "text=acmecorp" https://slack.example/api/chat.postMessage'),
]


class TestWrapperAndPathBypassesFailClosed:
    """F7.1/F7.3/F7.7: every wrapper/path/env/curl-form spelling of a public leak blocks."""

    @pytest.mark.parametrize(("label", "command"), _DENY_SPELLINGS, ids=[r[0] for r in _DENY_SPELLINGS])
    def test_spelling_is_detected_extracted_and_blocked(self, label: str, command: str, config: Path) -> None:
        # (a) DETECTION agrees it is a publish.
        assert is_publish_command(command) is True, f"detection missed: {label}"
        # (b) EXTRACTION yields the planted term or a fail-closed sentinel -- never
        # an empty payload the scanner reads as clean.
        payload = extract_bash_payload(command, fail_closed_body_file=True)
        assert "acmecorp" in payload or is_fail_closed_sentinel(payload), f"extraction empty: {label}"
        # (c) End-to-end the destination-aware gate BLOCKS.
        assert _verdict(command, config) == "block", f"not blocked: {label}"


# ── ALLOW rows: the over-block guard (as load-bearing as the DENY half) ──

_ALLOW_ROWS: list[tuple[str, str]] = [
    ("grep quotes forge spelling", 'grep "gh pr create" notes.md'),
    ("rg quotes sh -c gh", "rg 'sh -c \"gh\"' src/"),
    ("cat piped to grep glab", "cat notes.md | grep glab"),
    ("echo word containing gh", 'echo "nightlight weight is fine"'),
    ("ssh with no forge call", 'ssh host "echo done"'),
]


class TestReadOnlyInspectionNotOverBlocked:
    """Read-only inspection that merely QUOTES a forge token is never a publish."""

    @pytest.mark.parametrize(("label", "command"), _ALLOW_ROWS, ids=[r[0] for r in _ALLOW_ROWS])
    def test_inspection_is_not_a_publish_and_allows(self, label: str, command: str, config: Path) -> None:
        assert is_publish_command(command) is False, f"over-detected as publish: {label}"
        assert _verdict(command, config) == "allow", f"over-blocked: {label}"

    def test_commit_message_discussing_the_bypass_allows(self, config: Path) -> None:
        # A git commit is a publish surface, but a clean subject that merely
        # DISCUSSES the bypass (no synthetic banned term) must not block.
        cmd = 'git commit -m "harden the gh wrapper leak-gate bypass"'
        assert _verdict(cmd, config) == "allow"

    def test_private_repo_post_with_domain_word_allows(self, config: Path) -> None:
        cmd = 'gh pr create -R internalcorp/svc --title "feat" --body "acmecorp acmewidget rollout"'
        assert _verdict(cmd, config) == "allow"

    def test_clean_wrapper_post_allows(self, config: Path) -> None:
        # Never-lockout: the wrapper canon EXTRACTS the body (no fail-closed
        # sentinel for a transparent wrapper), so a CLEAN wrapper-hidden post
        # scans clean and allows. (A wrapper-hidden destination cannot be proven
        # private by the visibility gate, so a banned body there fails closed --
        # the conservative direction; only the clean common case must not block.)
        cmd = "xargs gh pr create -R internalcorp/svc --body 'a routine status update'"
        assert _verdict(cmd, config) == "allow"


# ── F7.4 unquoted / single-quoted $VAR bodies ──


class TestEnvVarBodyResolution:
    """F7.4: an unquoted live ``$VAR`` body is resolved; a single-quoted one stays inert."""

    def test_unquoted_var_with_banned_term_blocks(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEAKBODY", "ship to acmecorp")
        cmd = f"gh pr create {_PUBLIC} --title fix --body $LEAKBODY"
        assert _verdict(cmd, config) == "block"

    def test_unquoted_clean_var_allows(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLEANBODY", "a routine status update")
        cmd = f"gh pr create {_PUBLIC} --title fix --body $CLEANBODY"
        assert _verdict(cmd, config) == "allow"

    def test_unquoted_absent_var_fails_closed(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ABSENTBODY", raising=False)
        cmd = f"gh pr create {_PUBLIC} --title fix --body $ABSENTBODY"
        assert _verdict(cmd, config) == "block"

    def test_single_quoted_var_is_inert_literal(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A single-quoted '$VAR' is the published body itself (documenting a flag),
        # not an env reference -- bash never expands it, so a clean literal allows
        # even when an env var of that name holds a banned term.
        monkeypatch.setenv("LEAKBODY", "ship to acmecorp")
        cmd = f"gh pr create {_PUBLIC} --title fix --body '$LEAKBODY'"
        assert _verdict(cmd, config) == "allow"


# ── F7.5 heredoc terminator line-anchoring ──


class TestHeredocTerminatorAnchoring:
    """F7.5: a body line that BEGINS with the delimiter word no longer truncates the scan."""

    def test_mid_body_delimiter_word_does_not_truncate(self, config: Path) -> None:
        cmd = (
            f"gh pr create {_PUBLIC} --title fix --body-file - <<EOF\n"
            "release notes\n"
            "EOF and the rest mentions acmecorp\n"
            "trailing line\n"
            "EOF\n"
        )
        payload = extract_bash_payload(cmd, fail_closed_body_file=True)
        assert "acmecorp" in payload
        assert _verdict(cmd, config) == "block"


# ── F7.7 curl multipart @file / <file fail closed ──


class TestCurlFormFileFailsClosed:
    """F7.7: a ``curl -F name=@file`` / ``name=<file`` multipart value fails closed."""

    def test_curl_form_file_reference_fails_closed(self, config: Path) -> None:
        cmd = 'curl -F "document=@/tmp/leak.txt" https://slack.example/api/files.upload'
        payload = extract_bash_payload(cmd, fail_closed_body_file=True)
        assert is_fail_closed_sentinel(payload)

    def test_curl_form_inline_value_is_scanned(self, config: Path) -> None:
        cmd = 'curl -F "text=acmecorp leak" https://slack.example/api/chat.postMessage'
        payload = extract_bash_payload(cmd, fail_closed_body_file=True)
        assert "acmecorp" in payload


# ── secrets block on wrapper/path forms regardless of destination ──


class TestSecretsBlockOnWrapperForms:
    """A secret in a wrapper/path/api-field surface blocks on EVERY destination."""

    def test_secret_in_path_form_title_blocks(self, config: Path) -> None:
        cmd = f'/usr/bin/gh pr create -R internalcorp/svc -t "release {_FAKE_SECRET}"'
        assert _verdict(cmd, config) == "block"

    def test_secret_in_env_prefixed_api_field_blocks(self, config: Path) -> None:
        cmd = f"foo=1 gh api repos/internalcorp/svc/issues -f title={_FAKE_SECRET}"
        assert _verdict(cmd, config) == "block"
