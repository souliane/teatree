"""Tests for the banned-terms posting gate (#1415).

The detection module ``teatree.hooks.banned_terms_scanner`` and its
PreToolUse handler ``handle_banned_terms_pretool`` together promote the
commit-only ``check-banned-terms.sh`` hook to the non-commit posting
surfaces (``gh issue/pr create|edit|comment``, ``glab mr|issue
note|create``, the ``gh api`` / ``glab api`` REST paths). It is the
sibling of the #1213 quote-scanner gate: it reuses the exact same
``_command_parser`` publish-surface detection + body extraction, then
delegates the *matching* to the existing ``check-banned-terms.sh``
against the ``~/.teatree.toml`` term list — it does NOT reimplement
matching.

These tests exercise the gate via real hook invocation: a clean body
passes, a banned-term body blocks, ``--body-file`` is read from disk.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_banned_terms_pretool
from teatree.hooks import _command_parser, _repo_visibility, banned_terms_scanner
from teatree.hooks._command_parser import FAIL_CLOSED_SENTINEL


@pytest.fixture
def config(tmp_path: Path) -> Path:
    """A ``~/.teatree.toml`` shaped config carrying one banned term.

    Also declares the private-repo allowlist used by the #126 carve-out
    tests; the banned-terms scanner ignores the extra key.
    """
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        "[teatree]\n"
        'banned_terms = ["acmecorp"]\n'
        'private_repos = ["acmecorp-engineering"]\n'
        'internal_publish_namespaces = ["internalcorp", "acme-internal"]\n',
        encoding="utf-8",
    )
    return cfg


@pytest.fixture(autouse=True)
def _pin_config(config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the scanner at the test config instead of the real one."""
    monkeypatch.setenv("T3_BANNED_TERMS_CONFIG", str(config))


def _bash(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


def _private_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", "git@gitlab.com:acmecorp-engineering/product.git")
    return repo


def _public_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "public-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", "git@github.com:souliane/teatree.git")
    return repo


class TestScanText:
    def test_banned_term_is_matched(self, config: Path) -> None:
        term = banned_terms_scanner.scan_text("we ship to acmecorp next week", config_path=config)
        assert term == "acmecorp"

    def test_clean_text_returns_none(self, config: Path) -> None:
        assert banned_terms_scanner.scan_text("we ship next week", config_path=config) is None

    def test_match_is_case_insensitive(self, config: Path) -> None:
        # All-caps keeps it a single token (no camelCase boundary) so this
        # isolates case-insensitivity rather than the camelCase split.
        assert banned_terms_scanner.scan_text("ACMECORP ships", config_path=config) == "acmecorp"

    def test_email_only_match_is_ignored(self, config: Path) -> None:
        # Mirrors check-banned-terms.sh: a term only inside an email is allowed.
        text = "ping me at dev@acmecorp.example for details"
        assert banned_terms_scanner.scan_text(text, config_path=config) is None

    def test_empty_text_returns_none(self, config: Path) -> None:
        assert banned_terms_scanner.scan_text("", config_path=config) is None

    def test_missing_config_returns_none(self, tmp_path: Path) -> None:
        assert banned_terms_scanner.scan_text("acmecorp", config_path=tmp_path / "absent.toml") is None

    def test_fail_closed_sentinel_blocks(self, config: Path) -> None:
        # An unresolvable body (the sentinel) is not a configured term, so
        # delegating it to check-banned-terms.sh would clear it; it must BLOCK,
        # mirroring the quote / bare-reference sibling scanners.
        assert banned_terms_scanner.scan_text(FAIL_CLOSED_SENTINEL, config_path=config) is not None

    def test_fail_closed_sentinel_blocks_even_without_config(self, tmp_path: Path) -> None:
        assert banned_terms_scanner.scan_text(FAIL_CLOSED_SENTINEL, config_path=tmp_path / "absent.toml") is not None


class TestWholeTokenMatching:
    """The posting gate matches whole tokens, not substrings (over-block fix).

    A short configured term must not surface inside a longer unbroken word
    (the real failing case was a short term blocking a longer English word
    that merely contained it). The synthetic ``acme`` term proves the same
    class of bug without naming any real customer/overlay value.
    """

    @pytest.fixture
    def short_term_config(self, tmp_path: Path) -> Path:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text('[teatree]\nbanned_terms = ["acme", "acme-corp", "foo_bar"]\n', encoding="utf-8")
        return cfg

    @pytest.mark.parametrize("text", ["a cooperative effort", "pacme builds", "an acmeology lecture"])
    def test_single_word_substring_inside_a_word_does_not_block(self, short_term_config: Path, text: str) -> None:
        # The single-word ``acme`` must not surface inside a longer unbroken word.
        assert banned_terms_scanner.scan_text(text, config_path=short_term_config) is None

    @pytest.mark.parametrize(
        ("text", "expected"),
        # camelCase/Pascal split + lowercase-glued fallback for multi-word terms.
        [
            ("acmecorp ships next week", "acme-corp"),
            ("the acmeCorp service", "acme"),
            ("the AcmeCorp service", "acme"),
            ("a fooBar value", "foo_bar"),
        ],
    )
    def test_camelcase_and_glued_multiword_blocks(self, short_term_config: Path, text: str, expected: str) -> None:
        assert banned_terms_scanner.scan_text(text, config_path=short_term_config) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("rolling out acme today", "acme"),
            ("see x-acme-y in the diff", "acme"),
            ("Acme, hi there", "acme"),
            # ``acme`` is the first configured term and is a whole token of
            # ``acme-corp``, so it is the one reported — the block still fires.
            ("the acme-corp account", "acme"),
            ("set foo_bar = 1", "foo_bar"),
            ("the foo bar value", "foo_bar"),
        ],
    )
    def test_whole_token_run_blocks(self, short_term_config: Path, text: str, expected: str) -> None:
        assert banned_terms_scanner.scan_text(text, config_path=short_term_config) == expected

    def test_isolated_multi_token_term_blocks_and_is_reported(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text('[teatree]\nbanned_terms = ["acme-corp"]\n', encoding="utf-8")
        assert banned_terms_scanner.scan_text("the acme-corp account", config_path=cfg) == "acme-corp"


class TestMatchedTermAttribution:
    """``_matched_term`` attributes by whole-token match, never substring.

    A flagged line that contains a longer word must not be attributed to a
    short term that is merely its substring.
    """

    def test_attribution_is_not_a_substring_coincidence(self) -> None:
        # ``wid`` is a substring of ``widget`` but is NOT a whole token in the
        # flagged line, so the real whole-token term is reported instead.
        report = "BANNED TERM in /tmp/x.txt:\n  1:the acme widget shipped\n\nBanned terms: wid, acme\n"
        assert banned_terms_scanner._matched_term(report) == "acme"


class TestExtractSecretScanSurfaces:
    def test_title_long_flag_secret_is_surfaced(self) -> None:
        secret = "ghp_" + "A" * 40
        text = _command_parser.extract_secret_scan_text(f'gh pr create -R souliane/teatree --title "{secret}"')
        assert secret in text

    def test_short_title_flag_secret_is_surfaced(self) -> None:
        secret = "ghp_" + "A" * 40
        text = _command_parser.extract_secret_scan_text(f'gh pr create -R souliane/teatree -t "{secret}"')
        assert secret in text

    def test_api_non_body_field_secret_is_surfaced(self) -> None:
        secret = "ghp_" + "A" * 40
        text = _command_parser.extract_secret_scan_text(f"gh api repos/souliane/teatree/issues -f title={secret}")
        assert secret in text


class TestIsPublishCommandTokenAware:
    def test_api_after_interspersed_persistent_flag(self) -> None:
        assert _command_parser.is_publish_command("gh --hostname github.com api repos/o/r/issues -f body=x")

    def test_api_after_interspersed_method_flag(self) -> None:
        assert _command_parser.is_publish_command("gh -X POST api repos/o/r/issues -f body=x")

    def test_git_c_commit_is_detected(self) -> None:
        assert _command_parser.is_publish_command('git -C /some/dir commit -m "msg"')

    def test_git_global_dir_commit_long_message_is_detected(self) -> None:
        assert _command_parser.is_publish_command('git --git-dir=/x/.git --work-tree=/x commit --message "msg"')

    def test_flagless_git_commit_is_not_a_publish_surface(self) -> None:
        assert not _command_parser.is_publish_command("git -C /some/dir commit")

    def test_plain_non_publish_command_stays_false(self) -> None:
        assert not _command_parser.is_publish_command("git -C /some/dir status")


class TestApiEffectiveMethodCorpus:
    """`gh`/`glab api` is classified by EFFECTIVE HTTP method, not bare substring (#1530).

    A read-only `api` call (effective GET/HEAD) is NOT a publish, so the
    destination-aware gates no longer over-block it; an `api` WRITE (effective
    POST/PATCH/PUT/DELETE, last-wins on repeated `-X`/`--method`) stays a publish
    surface the body walkers must scan.
    """

    @pytest.mark.parametrize(
        "command",
        [
            # Bare reads — no method flag, no body flag → default GET.
            "gh api user",
            "gh api repos/o/r/commits/main",
            "glab api projects/42/merge_requests",
            # Explicit GET, even alongside a `-f` query param (#1568): a forced
            # GET sends `-f` as a query param, never a body write.
            "gh api repos/o/r/issues --method GET",
            "gh api repos/o/r/issues -X GET -f state=open",
            "glab api projects/42/issues --method=GET -f per_page=100",
            # No-space explicit GET forces a read.
            "gh api repos/o/r/issues -XGET -f state=open",
            # Repeated method flags, GET LAST → effective GET → read.
            "gh api repos/o/r/issues -X POST -X GET",
            # HEAD is a read.
            "gh api repos/o/r -X HEAD",
        ],
    )
    def test_read_only_api_is_not_a_publish(self, command: str) -> None:
        assert not _command_parser.is_publish_command(command)

    @pytest.mark.parametrize(
        "command",
        [
            # Body flag with no method → gh/glab default to POST → write.
            "gh api repos/o/r/issues -f title=bug -f body=x",
            "glab api projects/42/issues --field body=x",
            # Explicit write methods.
            "gh api repos/o/r/issues -X POST -f body=x",
            "gh api repos/o/r/issues/1 --method PATCH -f body=x",
            "glab api projects/42/merge_requests/7/notes -X PUT -f body=x",
            "gh api repos/o/r/issues/1 -X DELETE",
            # No-space write shorthand.
            "glab api projects/42/issues -XPOST -f body=x",
            # Repeated method flags, write LAST → effective write.
            "gh api repos/o/r/issues -X GET -X POST -f body=x",
            # Interspersed persistent flag before `api` still detected as write.
            "gh --hostname github.com api repos/o/r/issues -f body=x",
        ],
    )
    def test_write_api_stays_a_publish(self, command: str) -> None:
        assert _command_parser.is_publish_command(command)

    @pytest.mark.parametrize(
        "command",
        [
            "gh pr view 12",
            "gh pr list --state open",
            "gh pr diff 12",
            "gh repo view o/r",
        ],
    )
    def test_gh_pr_reads_are_not_a_publish(self, command: str) -> None:
        assert not _command_parser.is_publish_command(command)


class TestExtractPublishPayload:
    def test_gh_issue_create_body_is_extracted(self) -> None:
        payload = banned_terms_scanner.extract_publish_payload(
            "Bash", {"command": 'gh issue create --title t --body "ship to acmecorp"'}
        )
        assert payload is not None
        assert "acmecorp" in payload

    def test_non_publish_command_returns_none(self) -> None:
        assert banned_terms_scanner.extract_publish_payload("Bash", {"command": "ls -la"}) is None

    def test_non_bash_tool_returns_none(self) -> None:
        assert banned_terms_scanner.extract_publish_payload("Write", {"command": "x"}) is None

    def test_body_file_is_read_from_disk(self, tmp_path: Path) -> None:
        body_file = tmp_path / "body.md"
        body_file.write_text("internal note about acmecorp\n", encoding="utf-8")
        payload = banned_terms_scanner.extract_publish_payload(
            "Bash", {"command": f"gh pr create --title t --body-file {body_file}"}
        )
        assert payload is not None
        assert "acmecorp" in payload


class TestRelativeBodyFileResolvesAgainstCommandDir:
    """A relative ``--body-file`` resolves against the command's own ``cd`` dir.

    At PreToolUse the cold hook subprocess's cwd has reset away from the
    worktree, so a ``cd <worktree> && gh pr create --body-file body.md`` body
    file is unreadable from the cwd. The gate previously failed closed and
    blocked a clean post; it now resolves the relative path against the
    command's leading ``cd`` dir — clean passes, banned blocks.
    """

    def test_clean_relative_body_file_passes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "body.md").write_text("ship the docs refresh next week\n", encoding="utf-8")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        payload = banned_terms_scanner.extract_publish_payload(
            "Bash", {"command": f"cd {tmp_path} && gh pr create --title t --body-file body.md"}
        )
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL not in payload
        assert "ship the docs refresh" in payload

    def test_banned_relative_body_file_blocks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "body.md").write_text("internal note about acmecorp\n", encoding="utf-8")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        payload = banned_terms_scanner.extract_publish_payload(
            "Bash", {"command": f"cd {tmp_path} && gh pr create --title t --body-file body.md"}
        )
        assert payload is not None
        assert "acmecorp" in payload


class TestBodyFileWriteThenPostResolution:
    """An in-command write paired with a later ``--body-file <path>`` resolves.

    A ``printf``/``echo > path`` write paired with a later ``--body-file
    <path>`` in the SAME command resolves to the written body — the file does
    NOT exist on disk at PreToolUse scan time, so the gate must read the body
    from the writer's operands rather than fail closed on every body (the
    indirection-body bug). Safety is preserved: a ``--body-file`` whose body is
    NOT written in-command (a bare shell variable, a missing literal file)
    still fails closed.
    """

    def test_printf_redirect_to_var_path_resolves_clean_body(self) -> None:
        # ``f=$(mktemp); printf '%s' '<clean>' > "$f"; gh ... --body-file "$f"``.
        # The $f token is identical in the write and the reference, so the
        # resolver pairs them even though neither is expanded at scan time.
        cmd = 'f=$(mktemp); printf "%s" "ship next week" > "$f"; gh pr comment 5 --repo o/r --body-file "$f"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "ship next week" in payload
        assert FAIL_CLOSED_SENTINEL not in payload

    def test_echo_redirect_to_var_path_resolves_clean_body(self) -> None:
        cmd = 'f=$(mktemp); echo "ship next week" > "$f"; glab mr note create 7 --repo o/r --body-file "$f"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "ship next week" in payload
        assert FAIL_CLOSED_SENTINEL not in payload

    def test_attached_redirect_no_space_resolves_clean_body(self) -> None:
        # ``printf '%s' 'x' >"$f"`` — the unspaced redirect lexes as a single
        # glued ``>$f`` token; the target must still pair with --body-file "$f".
        cmd = 'f=$(mktemp); printf "%s" "ship next week" >"$f"; gh pr comment 5 --repo o/r --body-file "$f"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "ship next week" in payload
        assert FAIL_CLOSED_SENTINEL not in payload

    def test_append_redirect_resolves_clean_body(self) -> None:
        cmd = 'f=$(mktemp); printf "%s" "ship next week" >> "$f"; gh pr comment 5 --repo o/r --body-file "$f"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "ship next week" in payload
        assert FAIL_CLOSED_SENTINEL not in payload

    def test_printf_redirect_to_literal_path_resolves_clean_body(self, tmp_path: Path) -> None:
        body_file = tmp_path / "post-body.txt"
        cmd = f'printf "%s" "ship next week" > {body_file}; gh pr comment 5 --repo o/r --body-file {body_file}'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "ship next week" in payload
        assert FAIL_CLOSED_SENTINEL not in payload

    def test_write_then_post_with_banned_term_resolves_and_carries_term(self) -> None:
        # The gate checks the RESOLVED content, so a banned term written via
        # printf and posted via --body-file is surfaced for the matcher.
        cmd = 'f=$(mktemp); printf "%s" "ship to acmecorp" > "$f"; gh pr comment 5 --repo o/r --body-file "$f"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "acmecorp" in payload
        assert FAIL_CLOSED_SENTINEL not in payload

    def test_body_file_var_without_in_command_write_fails_closed(self) -> None:
        # No in-command write to $BODY and no on-disk file — genuinely
        # unresolvable, so the gate must STILL fail closed.
        cmd = 'gh pr comment 5 --repo o/r --body-file "$BODY"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL in payload

    def test_body_file_missing_literal_path_fails_closed(self) -> None:
        cmd = "gh pr comment 5 --repo o/r --body-file /no/such/body-file-xyz.txt"
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL in payload


class TestOverride:
    def test_flag_in_first_segment_bypasses(self) -> None:
        cmd = 'gh issue create --title t --body "acmecorp" --allow-banned-term'
        assert banned_terms_scanner.has_override("Bash", {"command": cmd}) is True

    def test_env_var_bypasses(self) -> None:
        tool_input = {"command": "gh issue create", "env": {"ALLOW_BANNED_TERM": "1"}}
        assert banned_terms_scanner.has_override("Bash", tool_input) is True

    def test_clean_command_has_no_override(self) -> None:
        assert banned_terms_scanner.has_override("Bash", {"command": "gh issue create --body x"}) is False

    def test_flag_after_metacharacter_does_not_bypass(self) -> None:
        # A flag smuggled into a second chained command must not bypass.
        cmd = 'gh issue create --body "acmecorp"; echo --allow-banned-term'
        assert banned_terms_scanner.has_override("Bash", {"command": cmd}) is False

    def test_non_bash_tool_has_no_flag_override(self) -> None:
        # A non-Bash tool can only override via the env mapping.
        assert banned_terms_scanner.has_override("Write", {"env": {"ALLOW_BANNED_TERM": "1"}}) is True
        assert banned_terms_scanner.has_override("Write", {}) is False


class TestScanTextFailOpen:
    def test_missing_script_fails_open(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(banned_terms_scanner, "_scanner_script", lambda: Path("/nonexistent/check.sh"))
        assert banned_terms_scanner.scan_text("acmecorp", config_path=config) is None

    def test_subprocess_error_fails_open(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError

        monkeypatch.setattr(banned_terms_scanner, "run_allowed_to_fail", _boom)
        assert banned_terms_scanner.scan_text("acmecorp", config_path=config) is None

    def test_scanner_crash_fails_open(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # An unexpected exit code (script itself failed) raises CommandFailedError
        # inside run_allowed_to_fail — the gate fails open rather than crash.
        def _crash(*_args: object, **_kwargs: object) -> None:
            raise banned_terms_scanner.CommandFailedError(["check"], 2, "", "boom")

        monkeypatch.setattr(banned_terms_scanner, "run_allowed_to_fail", _crash)
        assert banned_terms_scanner.scan_text("acmecorp", config_path=config) is None


class TestMatchedTerm:
    def test_term_in_flagged_line_is_reported(self) -> None:
        report = "BANNED TERM in /tmp/x.txt:\n  1:ship to acmecorp\n\nBanned terms: acmecorp, widgetco\n"
        assert banned_terms_scanner._matched_term(report) == "acmecorp"

    def test_falls_back_to_first_configured_term_when_no_flagged_line_matches(self) -> None:
        # The script reported a match but the term substring is not in any
        # flagged line we parsed — report the first configured term so the
        # deny reason is never empty.
        report = "BANNED TERM in /tmp/x.txt:\n\nBanned terms: acmecorp, widgetco\n"
        assert banned_terms_scanner._matched_term(report) == "acmecorp"

    def test_empty_report_returns_none(self) -> None:
        assert banned_terms_scanner._matched_term("") is None


@pytest.mark.integration
class TestHookHandlerEndToEnd:
    def test_clean_body_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash('gh issue create --title t --body "ship next week"'))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_banned_term_body_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash('gh issue create --title t --body "ship to acmecorp"'))
        assert blocked is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        assert "acmecorp" in decision["permissionDecisionReason"]

    def test_body_file_is_read_and_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        body_file = tmp_path / "issue_body.md"
        body_file.write_text("This affects acmecorp's deployment.\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(_bash(f"gh pr create --title t --body-file {body_file}"))
        assert blocked is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        assert "acmecorp" in decision["permissionDecisionReason"]

    def test_write_then_post_clean_body_via_body_file_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The indirection-body bug: a clean body materialised with printf into a
        # mktemp file and posted via --body-file used to fail closed on EVERY
        # body. It must now resolve and pass the gate.
        cmd = 'f=$(mktemp); printf "%s" "ship next week" > "$f"; gh pr comment 5 --repo o/r --body-file "$f"'
        blocked = handle_banned_terms_pretool(_bash(cmd))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_write_then_post_banned_body_via_body_file_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The gate checks the RESOLVED body content, not a placeholder, so a
        # banned term written via printf and posted via --body-file is blocked.
        cmd = 'f=$(mktemp); printf "%s" "ship to acmecorp" > "$f"; gh pr comment 5 --repo o/r --body-file "$f"'
        blocked = handle_banned_terms_pretool(_bash(cmd))
        assert blocked is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        assert "acmecorp" in decision["permissionDecisionReason"]

    def test_unresolvable_body_file_still_fails_closed(self, capsys: pytest.CaptureFixture[str]) -> None:
        # A --body-file whose body is NOT written in-command and is not on disk
        # is genuinely unresolvable — the gate must STILL block (fail closed),
        # never pass an unscanned public body.
        blocked = handle_banned_terms_pretool(_bash('gh pr comment 5 --repo o/r --body-file "$BODY"'))
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_unresolvable_body_deny_message_is_not_a_banned_term(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The false-positive: an unresolvable body used to surface the internal
        # sentinel as the "banned term", making the deny read "the body carries
        # the banned term '<unresolved publish body>'". The message must instead
        # explain the body could not be read.
        handle_banned_terms_pretool(_bash('gh pr comment 5 --repo o/r --body-file "$BODY"'))
        reason = json.loads(capsys.readouterr().out)["permissionDecisionReason"]
        assert "<unresolved publish body>" not in reason
        assert "banned term" not in reason
        assert "--allow-banned-term" not in reason

    def test_gh_short_body_file_flag_is_read_and_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ``gh issue/pr create|comment``'s ``-F`` is the short form of
        # ``--body-file``. A banned term in the ``-F`` file posted to a PUBLIC
        # target must be read and blocked, exactly like the long ``--body-file``
        # form. (RED before the fix: ``gh`` ``-F`` routed to the api-field
        # walker, the file went unread, and the term slipped through.)
        body_file = tmp_path / "issue_body.md"
        body_file.write_text("This affects acmecorp's deployment.\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(_bash(f"gh pr create --title t -F {body_file}"))
        assert blocked is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        assert "acmecorp" in decision["permissionDecisionReason"]

    def test_gh_short_body_file_flag_to_private_target_is_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Over-block guard: the SAME ``-F`` file posted to a provably-private
        # ``-R`` target (in the internal_publish_namespaces allowlist) is
        # skipped by the destination gate before the payload is scanned, so a
        # private repo's own domain words are allowed.
        body_file = tmp_path / "issue_body.md"
        body_file.write_text("This affects acmecorp's deployment.\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(_bash(f"gh pr create -R internalcorp/svc --title t -F {body_file}"))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_gh_attached_short_body_file_flag_is_read_and_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The attached short-option spelling ``-F<path>`` (no space) is also the
        # ``--body-file`` form: its file body is read and a banned term blocks.
        body_file = tmp_path / "issue_body.md"
        body_file.write_text("This affects acmecorp's deployment.\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(_bash(f"gh pr create --title t -F{body_file}"))
        assert blocked is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        assert "acmecorp" in decision["permissionDecisionReason"]

    def test_gh_short_field_assignment_stays_an_api_field(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Disambiguation guard: a ``gh api -F body=...`` field assignment is NOT
        # a file reference; the banned term in the inline field value is still
        # scanned and blocks, unchanged by the ``-F`` body-file handling.
        blocked = handle_banned_terms_pretool(_bash("gh api repos/souliane/teatree/issues -F title=t -F body=acmecorp"))
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_gh_pr_comment_with_banned_term_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash('gh pr comment 5 --body "acmecorp asked for this"'))
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_glab_mr_note_with_banned_term_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash('glab mr note 5 --message "acmecorp wants this"'))
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_override_flag_bypasses_block(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash('gh issue create --title t --body "acmecorp" --allow-banned-term'))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_non_publish_command_is_noop(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash("ls -la"))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_empty_body_publish_is_allowed(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash("gh issue create --title t"))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_missing_config_fails_open(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("T3_BANNED_TERMS_CONFIG", "/nonexistent/.teatree.toml")
        blocked = handle_banned_terms_pretool(_bash('gh issue create --body "acmecorp"'))
        assert blocked is False
        assert capsys.readouterr().out == ""


@pytest.mark.integration
class TestCleanPublishFormsMustNotBlock:
    """Every common publish form with a clean body MUST pass the gate.

    These guard the non-sentinel pass-through path: a clean inline body or a
    readable body file must never be routed through a blocking path. They are
    forward-looking coverage, not the #182 regression guard — each would also
    pass on pre-fix code (the bug only fired on an *unresolvable* body). The
    anti-vacuous RED-before-fix guard for issue #182 is
    ``TestHookHandlerEndToEnd.test_unresolvable_body_deny_message_is_not_a_banned_term``.
    Each form below is paired with a must-FLAG counterpart in
    ``TestBannedTermPublishFormsMustBlock`` so the guards are two-sided.
    """

    @pytest.fixture(autouse=True)
    def _isolated_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))

    def test_git_commit_inline_m_clean_body_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash('git commit -m "feat: ship faster builds"'))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_git_commit_inline_message_clean_body_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash('git commit --message "fix: resolve timeout in retry loop"'))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_git_commit_file_absolute_path_clean_body_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body_file = tmp_path / "commit_msg.txt"
        body_file.write_text("feat: improve throughput\n\nDetails here.\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(_bash(f"git commit -F {body_file}"))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_git_commit_long_file_flag_absolute_path_clean_body_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body_file = tmp_path / "commit_msg.txt"
        body_file.write_text("chore: bump dependencies\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(_bash(f"git commit --file {body_file}"))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_git_commit_c_flag_with_inline_m_clean_body_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash('git -C /some/worktree commit -m "refactor: extract helper"'))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_gh_pr_create_inline_body_clean_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash('gh pr create --title "feat" --body "Clean PR body"'))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_gh_pr_create_body_file_absolute_path_clean_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body_file = tmp_path / "pr_body.md"
        body_file.write_text("## Summary\n\nClean description.\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(_bash(f"gh pr create --title t --body-file {body_file}"))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_gh_issue_create_inline_body_clean_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(
            _bash('gh issue create --title "Bug report" --body "Steps to reproduce..."')
        )
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_gh_issue_create_body_file_absolute_path_clean_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body_file = tmp_path / "issue_body.md"
        body_file.write_text("## Description\n\nReproduction steps.\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(_bash(f"gh issue create --title t --body-file {body_file}"))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_glab_mr_create_inline_description_clean_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash('glab mr create --title "feat" --description "Clean MR body."'))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_glab_mr_create_description_file_absolute_path_clean_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body_file = tmp_path / "mr_body.md"
        body_file.write_text("## Summary\n\nThis MR adds a feature.\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(_bash(f"glab mr create --title t --description-file {body_file}"))
        assert blocked is False
        assert capsys.readouterr().out == ""


@pytest.mark.integration
class TestBannedTermPublishFormsMustBlock:
    """The must-FLAG counterpart of ``TestCleanPublishFormsMustNotBlock``.

    A real banned term in the same publish form must still be caught after
    the fix — the fix must not weaken detection, only eliminate the sentinel
    false-positive.
    """

    @pytest.fixture(autouse=True)
    def _isolated_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))

    def test_git_commit_inline_m_banned_term_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        repo = _public_repo(tmp_path)
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "fix the acmecorp issue"'},
            "cwd": str(repo),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_git_commit_file_absolute_path_banned_term_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _public_repo(tmp_path)
        body_file = tmp_path / "commit_msg.txt"
        body_file.write_text("feat: ship acmecorp feature\n", encoding="utf-8")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": f"git commit -F {body_file}"},
            "cwd": str(repo),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_gh_pr_create_inline_body_banned_term_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash('gh pr create --title "feat" --body "Deploy to acmecorp cluster"'))
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_gh_pr_create_body_file_absolute_path_banned_term_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body_file = tmp_path / "pr_body.md"
        body_file.write_text("This PR fixes the acmecorp integration.\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(_bash(f"gh pr create --title t --body-file {body_file}"))
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_gh_issue_create_inline_body_banned_term_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(
            _bash('gh issue create --title "Bug" --body "acmecorp reports this error"')
        )
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_gh_issue_create_body_file_absolute_path_banned_term_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body_file = tmp_path / "issue_body.md"
        body_file.write_text("acmecorp's deployment is broken.\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(_bash(f"gh issue create --title t --body-file {body_file}"))
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_glab_mr_create_inline_description_banned_term_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(
            _bash('glab mr create --title "feat" --description "Update acmecorp config"')
        )
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_glab_mr_create_description_file_absolute_path_banned_term_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body_file = tmp_path / "mr_body.md"
        body_file.write_text("This MR updates the acmecorp adapter.\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(_bash(f"glab mr create --title t --description-file {body_file}"))
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"


class TestHookChainRegistration:
    def test_handler_is_wired_before_skill_load(self) -> None:
        chain = router._HANDLERS["PreToolUse"]
        names = [h.__name__ for h in chain]
        assert "handle_banned_terms_pretool" in names
        assert names.index("handle_banned_terms_pretool") < names.index("handle_enforce_skill_loading")


class TestLeadingEnvOverride:
    """A leading ``ALLOW_BANNED_TERM=1`` env-assignment token bypasses the gate (#1415).

    The Claude Code harness forwards neither an inline ``env`` block nor a
    ``--allow-banned-term`` flag glab/gh would accept; the one spelling that
    reliably reaches the gate is a leading inline env-assignment on the
    command itself.
    """

    def test_leading_env_assignment_bypasses(self) -> None:
        cmd = 'ALLOW_BANNED_TERM=1 glab mr note 5 --message "ship to acmecorp"'
        assert banned_terms_scanner.has_override("Bash", {"command": cmd}) is True

    def test_leading_env_assignment_zero_does_not_bypass(self) -> None:
        cmd = 'ALLOW_BANNED_TERM=0 glab mr note 5 --message "acmecorp"'
        assert banned_terms_scanner.has_override("Bash", {"command": cmd}) is False

    def test_env_assignment_after_command_name_does_not_bypass(self) -> None:
        # Once the command name is reached, a later ``KEY=val``-shaped token
        # is an argument, not an inline env assignment.
        cmd = 'gh issue create --body "acmecorp" --field ALLOW_BANNED_TERM=1'
        assert banned_terms_scanner.has_override("Bash", {"command": cmd}) is False

    def test_env_assignment_after_separator_does_not_bypass(self) -> None:
        cmd = 'gh issue create --body "acmecorp"; ALLOW_BANNED_TERM=1 echo done'
        assert banned_terms_scanner.has_override("Bash", {"command": cmd}) is False

    def test_leading_env_assignment_behind_cd_prefix_bypasses(self) -> None:
        # The common sub-agent shape: cd into the worktree, THEN commit with the
        # override. Bash applies the assignment to the second segment's command,
        # so the override leads the segment that actually carries the publish.
        cmd = 'cd /work/ticket && ALLOW_BANNED_TERM=1 git commit -m "ship to acmecorp"'
        assert banned_terms_scanner.has_override("Bash", {"command": cmd}) is True

    def test_leading_env_assignment_behind_env_nav_prefix_bypasses(self) -> None:
        cmd = 'GIT_PAGER=cat ALLOW_BANNED_TERM=1 git commit -m "acmecorp"'
        assert banned_terms_scanner.has_override("Bash", {"command": cmd}) is True

    def test_override_segment_zero_value_does_not_bypass(self) -> None:
        cmd = 'cd /work && ALLOW_BANNED_TERM=0 git commit -m "acmecorp"'
        assert banned_terms_scanner.has_override("Bash", {"command": cmd}) is False

    def test_chained_segment_without_override_does_not_bypass(self) -> None:
        # The override leads ONLY the first (harmless echo) segment; the publish
        # segment carries no override, so the gate must still fire on it.
        cmd = 'ALLOW_BANNED_TERM=1 echo hi && gh issue create --body "acmecorp"'
        assert banned_terms_scanner.has_override("Bash", {"command": cmd}) is False

    @pytest.mark.integration
    def test_leading_env_assignment_bypasses_block_end_to_end(self, capsys: pytest.CaptureFixture[str]) -> None:
        cmd = 'ALLOW_BANNED_TERM=1 gh issue create --title t --body "ship to acmecorp"'
        blocked = handle_banned_terms_pretool(_bash(cmd))
        assert blocked is False
        assert capsys.readouterr().out == ""


@pytest.mark.integration
class TestDestinationAwareGate:
    """The gate scans only PUBLIC targets (#1415 destination-awareness).

    FAIL-CLOSED: a banned term posted to the genuinely-public
    ``souliane/teatree`` is still blocked; the same term posted to a
    configured internal namespace is allowed; an unresolvable destination
    stays blocked.
    """

    def test_banned_term_to_public_repo_is_blocked(self, capsys: pytest.CaptureFixture[str]) -> None:
        cmd = 'gh pr create -R souliane/teatree --title t --body "ship to acmecorp"'
        blocked = handle_banned_terms_pretool(_bash(cmd))
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_banned_term_to_internal_namespace_is_allowed(self, capsys: pytest.CaptureFixture[str]) -> None:
        cmd = 'gh pr create -R internalcorp/private-svc --title t --body "ship to acmecorp"'
        blocked = handle_banned_terms_pretool(_bash(cmd))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_internal_glab_api_raw_rest_is_scanned_not_skipped(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Raw-REST ``gh api`` / ``glab api`` can target any surface (custom
        # host, method, endpoint), so the destination gate never SKIPS an
        # api segment even when its URL path resolves to an internal
        # project -- mirroring the carve-out, which excludes api from its
        # eligible verbs. The over-scan is recoverable via --allow-banned-term.
        cmd = "glab api projects/internalcorp%2Fprivate-svc/issues -f body=acmecorp"
        blocked = handle_banned_terms_pretool(_bash(cmd))
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_banned_term_unparseable_destination_still_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        # A Slack-bound ``chat.postMessage`` curl has no resolvable repo
        # destination → PUBLIC (fail-closed) → still blocked.
        cmd = "curl -d text=acmecorp https://slack.com/api/chat.postMessage"
        blocked = handle_banned_terms_pretool(_bash(cmd))
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"


class TestFormatBlockMessage:
    def test_message_names_the_term_and_the_override(self) -> None:
        message = banned_terms_scanner.format_block_message("acmecorp")
        assert "acmecorp" in message
        assert "--allow-banned-term" in message

    def test_unresolvable_body_message_is_distinct_from_banned_term_message(self) -> None:
        message = banned_terms_scanner.format_unresolvable_body_message()
        assert "acmecorp" not in message
        assert "<unresolved publish body>" not in message
        assert "banned term" not in message

    def test_unresolvable_body_message_is_actionable(self) -> None:
        message = banned_terms_scanner.format_unresolvable_body_message()
        assert "body" in message.lower()
        assert "--allow-banned-term" not in message


@pytest.mark.integration
class TestPrivateRepoCarveOut:
    """A private-repo commit with the repo's own domain word is ALLOWED (#126)."""

    @pytest.fixture(autouse=True)
    def _isolated_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))

    def test_private_repo_commit_with_domain_word_is_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _private_repo(tmp_path)
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "fix the acmecorp refinery"'},
            "cwd": str(repo),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False  # downgraded to warn, not denied
        assert capsys.readouterr().out == ""  # no deny JSON on stdout

    def test_private_repo_commit_bodyfile_relative_path_reset_cwd_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The real cold-hook failure: a ``git -C <worktree> commit -F <relpath>``
        # where the harness cwd has reset AWAY from the worktree. The body file
        # lives INSIDE the private worktree and carries the repo's own domain
        # word. Reading the ``-F`` path against the reset cwd fails, so the
        # parser fail-closes to the sentinel and the carve-out never consults
        # the private origin -> a false hard-block. The body file IS resolvable
        # from the commit's own repo dir, so the carve-out must downgrade.
        repo = _private_repo(tmp_path)
        (repo / "commit_body.txt").write_text("fix the acmecorp refinery\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)  # reset-away cwd, not the worktree
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": f"git -C {repo} commit -F commit_body.txt"},
            "cwd": str(tmp_path),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False  # downgraded to warn, not denied
        assert capsys.readouterr().out == ""  # no deny JSON on stdout

    def test_public_repo_commit_bodyfile_relative_path_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Regression guard symmetric to the fix: the same ``-F <relpath>`` shape
        # whose body the gate now resolves from the commit's repo dir must STILL
        # hard-block when that repo is PUBLIC. The resolution fix must not weaken
        # the real protection -- a banned term in a body file committed to a
        # public repo is a leak.
        repo = tmp_path / "pub"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "remote", "add", "origin", "https://github.com/some/public.git")
        (repo / "commit_body.txt").write_text("ship to acmecorp\n", encoding="utf-8")
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        monkeypatch.chdir(tmp_path)
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": f"git -C {repo} commit -F commit_body.txt"},
            "cwd": str(tmp_path),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_commit_bodyfile_genuinely_missing_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A ``-F`` path that exists NOWHERE (not in cwd, not in the repo dir)
        # is a genuinely unresolvable body: it must keep failing closed even on
        # a private repo, so the resolution fallback never masks an unscannable
        # body. This preserves the #1207 fail-closed sentinel contract.
        repo = _private_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": f"git -C {repo} commit -F does_not_exist.txt"},
            "cwd": str(tmp_path),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_public_repo_commit_with_banned_term_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "pub"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "remote", "add", "origin", "https://github.com/some/public.git")
        # No allowlist hit; the visibility probe finds nothing → unknown →
        # NOT private → hard-block stands.
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "ship to acmecorp"'},
            "cwd": str(repo),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_private_repo_posting_command_with_cwd_target_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # gh issue create (no --repo flag) from a private CWD resolves the
        # target from the CWD origin; the allowlisted-private destination is
        # skipped by the destination gate (#1672) -- allowed, no deny.
        repo = _private_repo(tmp_path)
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": 'gh issue create --title t --body "ship to acmecorp"'},
            "cwd": str(repo),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False
        assert capsys.readouterr().out == ""  # no deny JSON on stdout

    def test_posting_command_with_explicit_public_repo_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # An explicit --repo pointing at a PUBLIC repo must never be carved out
        # regardless of what the CWD is. This is the load-bearing safety test.
        repo = _private_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": 'gh pr create --repo souliane/teatree --title t --body "ship to acmecorp"'},
            "cwd": str(repo),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_public_repo_commit_with_cd_prefixed_override_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The escape hatch behind the common cd-prefixed shape: even on a PUBLIC
        # repo the operator's explicit ALLOW_BANNED_TERM=1 must bypass the gate,
        # exactly as the leading form already does. Before the fix the override
        # on the second segment was dropped and the commit hard-blocked.
        repo = tmp_path / "pub"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "remote", "add", "origin", "https://github.com/some/public.git")
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": f'cd {repo} && ALLOW_BANNED_TERM=1 git commit -m "ship to acmecorp"'},
            "cwd": str(repo),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_slug_for_cwd_fails_safe_when_git_binary_is_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The cold hook subprocess can inherit a restricted PATH where ``git``
        # does not resolve, so ``git remote get-url`` raises FileNotFoundError.
        # An uncaught error would propagate out of the carve-out and crash the
        # whole gate; the slug resolver must fail SAFE to an empty slug exactly
        # as it already does for a CommandFailedError.
        repo = _private_repo(tmp_path)
        monkeypatch.setattr(
            _repo_visibility.git,
            "remote_url",
            lambda repo=".", remote="origin": (_ for _ in ()).throw(FileNotFoundError("git")),
        )
        assert _repo_visibility.slug_for_cwd(repo) == ""

    def test_private_repo_commit_downgrades_when_probe_binary_is_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The offline allowlist is the mechanism that exists precisely because the
        # live gh/glab visibility probe is unreliable in-hook (restricted PATH).
        # With the probe disabled, the slug still resolves from git and the
        # allowlist alone must downgrade the private repo's own-domain commit.
        repo = _private_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "fix the acmecorp refinery"'},
            "cwd": str(repo),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False  # downgraded to warn via the offline allowlist
        assert capsys.readouterr().out == ""

    def test_private_repo_commit_with_cd_prefixed_override_is_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The exact reported shape: cd into the private worktree, then commit
        # with the override. The override leads the SECOND (publish) segment, so
        # it must bypass the gate just like the leading form does.
        repo = _private_repo(tmp_path)
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": f'cd {repo} && ALLOW_BANNED_TERM=1 git commit -m "ship to acmecorp"'},
            "cwd": str(repo),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False
        assert capsys.readouterr().out == ""
