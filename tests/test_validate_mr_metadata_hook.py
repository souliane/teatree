"""Tests for the validate-mr-metadata PreToolUse hook (#119 Part 3).

The hook was a permanent no-op because it was gated behind
``T3_MR_VALIDATE_SCRIPT``, which is never set anywhere. The fix makes it
invoke ``t3 tool validate-mr`` (the active overlay's ``validate_pr``) BY
DEFAULT so a bad MR title/description is rejected BEFORE the push, every
time, with no opt-in. The env var remains an optional override.
"""

import json
import subprocess
from unittest.mock import patch

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_validate_mr_metadata
from teatree.core.mr_metadata import validate_mr_metadata
from teatree.types import DEFAULT_MR_TITLE_REGEX


def _verdict(command: str) -> list[str] | None:
    """``validate_mr_metadata`` verdict for *command* (``None`` when gate skips)."""
    fields = router._extract_mr_fields({"tool_name": "Bash", "tool_input": {"command": command}})
    if fields is None:
        return None
    return validate_mr_metadata(fields[0], fields[1], DEFAULT_MR_TITLE_REGEX)


def _glab_create(title: str, description: str) -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {
            "command": f"glab mr create --title '{title}' --description '{description}'",
        },
    }


def _fields(command: str) -> tuple[str, str]:
    """Extract (title, description) for *command*, asserting it IS an MR mutation."""
    result = router._extract_mr_fields({"tool_name": "Bash", "tool_input": {"command": command}})
    assert result is not None
    return result


class TestDefaultOverlayValidation:
    """No T3_MR_VALIDATE_SCRIPT set -> validate via `t3 tool validate-mr`."""

    def test_blocks_when_overlay_validator_rejects(self, monkeypatch, capsys):
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")

        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Title is empty.\nMR description is empty.\n"
        )
        with patch.object(router.subprocess, "run", return_value=completed) as run:
            blocked = handle_validate_mr_metadata(_glab_create("", ""))

        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        assert "Title is empty." in out["permissionDecisionReason"]
        # Invoked the default `t3 tool validate-mr` path.
        argv = run.call_args[0][0]
        assert argv[:3] == ["/usr/local/bin/t3", "tool", "validate-mr"]
        assert "--title" in argv
        assert "--description" in argv

    def test_allows_when_overlay_validator_passes(self, monkeypatch):
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(router.subprocess, "run", return_value=ok):
            blocked = handle_validate_mr_metadata(_glab_create("fix: x (p#1)", "fix: x (p#1)"))
        assert blocked is False

    def test_noop_when_not_a_glab_mr_command(self, monkeypatch):
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        data = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
        assert handle_validate_mr_metadata(data) is False

    def test_fails_closed_when_t3_not_on_path(self, monkeypatch, capsys):
        # No validator resolvable -> the gate FAILS CLOSED (deny), not open:
        # a non-compliant title must never reach GitLab just because the env
        # could not validate it. The escape hatch is the explicit env var.
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.delenv("T3_MR_VALIDATE_ALLOW_BROKEN_ENV", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: None)
        blocked = handle_validate_mr_metadata(_glab_create("bad", "bad"))
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        assert "validate" in out["permissionDecisionReason"].lower()

    def test_broken_env_escape_hatch_fails_open(self, monkeypatch):
        # The deliberate self-rescue opt-in: when the operator sets
        # T3_MR_VALIDATE_ALLOW_BROKEN_ENV, an unresolvable validator falls
        # back to fail-open so a genuinely broken env is not a hard deadlock.
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setenv("T3_MR_VALIDATE_ALLOW_BROKEN_ENV", "1")
        monkeypatch.setattr(router.shutil, "which", lambda _: None)
        assert handle_validate_mr_metadata(_glab_create("bad", "bad")) is False

    def test_fails_closed_when_validator_times_out(self, monkeypatch, capsys):
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.delenv("T3_MR_VALIDATE_ALLOW_BROKEN_ENV", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        with patch.object(router.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="t3", timeout=10)):
            blocked = handle_validate_mr_metadata(_glab_create("fix: x (p#1)", "fix: x (p#1)"))
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"

    def test_fails_closed_when_validator_binary_missing(self, monkeypatch, capsys):
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.delenv("T3_MR_VALIDATE_ALLOW_BROKEN_ENV", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        with patch.object(router.subprocess, "run", side_effect=FileNotFoundError):
            blocked = handle_validate_mr_metadata(_glab_create("fix: x (p#1)", "fix: x (p#1)"))
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"

    def test_missing_title_is_validated_not_skipped(self, monkeypatch, capsys):
        # An MR create with no --title is exactly the bad metadata the gate
        # must reject — it must be validated, not silently skipped (#119).
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {"tool_name": "Bash", "tool_input": {"command": "glab mr create --description 'x'"}}
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Title is empty.")
        with patch.object(router.subprocess, "run", return_value=rejected) as run:
            blocked = handle_validate_mr_metadata(data)
        assert blocked is True
        argv = run.call_args[0][0]
        assert argv[:3] == ["/usr/local/bin/t3", "tool", "validate-mr"]


class TestFileBasedDescriptionIsRead:
    """A file-based MR description (`-F`/`--description-file`) is read, not "".

    The inline ``--description 'x'`` regex captures nothing for a file-based
    description, so the gate previously validated an empty string and a
    non-compliant first line slipped through and failed CI downstream.
    """

    def test_extract_reads_description_file(self, tmp_path):
        desc = tmp_path / "d.md"
        desc.write_text("config(ci): real first line (proj#1)\n\nbody\n", encoding="utf-8")
        title, description = _fields(f"glab mr create --title 'config(ci): t' -F {desc}")
        assert title == "config(ci): t"
        assert description.startswith("config(ci): real first line")

    def test_extract_reads_long_description_file_flag(self, tmp_path):
        desc = tmp_path / "d.md"
        desc.write_text("fix: real (proj#1)\n", encoding="utf-8")
        _title, description = _fields(f"glab mr create --title 'fix: t' --description-file {desc}")
        assert description.startswith("fix: real")

    def test_missing_file_falls_back_to_empty_not_crash(self):
        # Unreadable file => "" (the validator then rejects the empty first
        # line — the correct verdict — rather than the gate crashing).
        title, description = _fields("glab mr create --title 'fix: t' -F /no/such/file.md")
        assert title == "fix: t"
        assert description == ""


class TestOutOfBandApiEditIsGated:
    """A REST-API MR/PR write is validated too, not just the create/update CLI.

    A description set via ``glab api --method PUT .../merge_requests/N
    --field description=…`` bypasses the ``glab mr create`` surface entirely.
    The gate now intercepts the API write and validates the fields it sets.
    """

    def test_bad_description_via_api_put_is_validated(self, monkeypatch):
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        cmd = "glab api --method PUT projects/x%2Fy/merge_requests/123 --field 'description=bad prose'"
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Invalid first line.")
        with patch.object(router.subprocess, "run", return_value=rejected) as run:
            blocked = handle_validate_mr_metadata({"tool_name": "Bash", "tool_input": {"command": cmd}})
        assert blocked is True
        argv = run.call_args[0][0]
        assert argv[:3] == ["/usr/local/bin/t3", "tool", "validate-mr"]
        # The untouched title is back-filled with the (here bad) description so
        # the verdict reflects only the edited field, never a spurious
        # "title empty" for an unset title.
        assert "bad prose" in argv

    def test_description_only_api_edit_does_not_force_validate_title(self):
        # The untouched title is mirrored from the set description, so a
        # description-only edit can never false-block on "Title is empty."
        title, description = _fields(
            "glab api --method PUT projects/x%2Fy/merge_requests/123 --field 'description=config(ci): real (proj#1)'"
        )
        assert title == "config(ci): real (proj#1)"
        assert description == "config(ci): real (proj#1)"

    def test_state_only_api_edit_is_skipped(self):
        # No title/description field touched => nothing to validate
        # (never-lockout: a partial state edit must not be force-validated).
        assert (
            router._extract_mr_fields(
                {
                    "tool_name": "Bash",
                    "tool_input": {
                        "command": "glab api --method PUT projects/x%2Fy/merge_requests/123 --field state_event=close"
                    },
                }
            )
            is None
        )

    def test_api_get_read_is_not_a_write(self):
        assert (
            router._extract_mr_fields(
                {"tool_name": "Bash", "tool_input": {"command": "glab api projects/x%2Fy/merge_requests/123"}}
            )
            is None
        )

    def test_gh_api_pr_create_is_gated(self, monkeypatch):
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        cmd = "gh api repos/o/r/pulls --method POST -f 'title=bad title' -f 'body=bad body'"
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="bad")
        with patch.object(router.subprocess, "run", return_value=rejected):
            blocked = handle_validate_mr_metadata({"tool_name": "Bash", "tool_input": {"command": cmd}})
        assert blocked is True


class TestMrTargetRepoIsThreadedToValidator:
    """The MR TARGET repo is parsed and passed as ``validate-mr --repo <slug>``.

    The cwd-keyed validator validates an MR against whatever overlay owns the
    agent's *current directory* — for a dispatched agent that is the clone of a
    different overlay than the one the MR targets, so the target overlay's
    rules are never applied. The gate must parse the MR's target (``-R``/``--repo``
    on ``glab mr``, the ``glab api`` namespace, the ``gh api repos/<o>/<r>``
    path) and thread it to the validator so the target overlay's rules govern
    regardless of cwd. ``strict-org/widget`` stands for the target repo.
    """

    def _argv_for(self, monkeypatch, command: str) -> list[str]:
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(router.subprocess, "run", return_value=ok) as run:
            handle_validate_mr_metadata({"tool_name": "Bash", "tool_input": {"command": command}})
        return list(run.call_args[0][0])

    def test_glab_mr_create_dash_r_flag_target_is_passed(self, monkeypatch):
        argv = self._argv_for(
            monkeypatch,
            "glab mr create -R strict-org/widget --title 'fix(x): t' --description 'fix(x): t'",
        )
        assert "--repo" in argv
        assert "strict-org/widget" in argv

    def test_glab_mr_create_long_repo_flag_target_is_passed(self, monkeypatch):
        argv = self._argv_for(
            monkeypatch,
            "glab mr create --repo strict-org/widget --title 'fix(x): t' --description 'fix(x): t'",
        )
        assert argv.count("--repo") == 1
        assert "strict-org/widget" in argv

    def test_glab_api_namespace_is_decoded_and_passed(self, monkeypatch):
        argv = self._argv_for(
            monkeypatch,
            "glab api --method POST projects/strict-org%2Fwidget/merge_requests "
            "--field 'title=fix(x): t' --field 'description=fix(x): t'",
        )
        assert "--repo" in argv
        assert "strict-org/widget" in argv

    def test_gh_api_pulls_path_target_is_passed(self, monkeypatch):
        argv = self._argv_for(
            monkeypatch,
            "gh api repos/souliane/teatree/pulls --method POST -f 'title=fix: t' -f 'body=fix: t'",
        )
        assert "--repo" in argv
        assert "souliane/teatree" in argv

    def test_no_parseable_target_appends_no_repo_flag(self, monkeypatch):
        # cwd-keyed fallback preserved: a bare create with no target flag must
        # NOT carry a --repo, so today's behaviour is unchanged.
        argv = self._argv_for(
            monkeypatch,
            "glab mr create --title 'fix: t' --description 'fix: t'",
        )
        assert "--repo" not in argv


class TestEnvVarOverrideStillWorks:
    """An explicitly-set T3_MR_VALIDATE_SCRIPT remains the override path."""

    def test_uses_script_when_env_var_set(self, monkeypatch, tmp_path):
        script = tmp_path / "v.py"
        script.write_text("import sys; sys.exit(0)", encoding="utf-8")
        monkeypatch.setenv("T3_MR_VALIDATE_SCRIPT", str(script))
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(router.subprocess, "run", return_value=ok) as run:
            blocked = handle_validate_mr_metadata(_glab_create("fix: x (p#1)", "body"))
        assert blocked is False
        # Used the script, not `t3 tool validate-mr`.
        argv = run.call_args[0][0]
        assert str(script) in argv
        assert "validate-mr" not in argv


class TestUpdateValidatesOnlySetFields:
    """``glab mr update`` must validate only the field(s) it actually sets.

    It over-fired by demanding BOTH a title and a description on every update, so
    a reviewer-only or single-field edit was force-validated against an empty
    sibling and blocked. Mirrors the out-of-band API path's never-lockout shape.
    """

    def test_metadata_only_update_is_skipped(self):
        for cmd in (
            "glab mr update 7624 --reviewer WouterLachat",
            "glab mr update --add-label needs-review",
            "glab mr update 12 --ready",
        ):
            assert router._extract_mr_fields({"tool_name": "Bash", "tool_input": {"command": cmd}}) is None

    def test_title_only_update_does_not_demand_a_description(self):
        # Updating only the title must not block on a missing What/Why body.
        assert _verdict("glab mr update --title 'fix: rename widget (proj#1)'") == []

    def test_title_only_update_still_catches_a_bad_title(self):
        assert _verdict("glab mr update --title 'rename widget'")

    def test_description_only_update_validates_the_description(self):
        good = "config(ci): real first line (proj#1)\n\n## What\nbody"
        assert _verdict(f"glab mr update --description '{good}'") == []

    def test_description_only_update_catches_a_bad_first_line(self):
        bad = "Summary of changes\n\n## What\nbody"
        assert _verdict(f"glab mr update --description '{bad}'")

    def test_update_with_both_fields_validates_both(self):
        assert _verdict("glab mr update --title 'fix: x (proj#1)' --description 'not conventional'")


class TestDynamicValueIsSkipped:
    """A double-quoted ``$(…)``/``$VAR``/backtick value is skipped, not validated.

    The PreToolUse hook sees the command BEFORE the shell expands it, so such a
    value is not the real one (a nested quote truncates it to e.g. ``$(cat ``).
    Validating the captured literal fragment false-blocks; the remote CI gate is
    the backstop.
    """

    def _fields_for(self, command: str):
        return router._extract_mr_fields({"tool_name": "Bash", "tool_input": {"command": command}})

    def test_command_substitution_description_is_skipped(self):
        cmd = 'glab mr create --title \'techdebt(x): real (proj#1)\' --description "$(cat "$DESC")"'
        assert self._fields_for(cmd) is None

    def test_variable_expansion_description_is_skipped(self):
        cmd = "glab mr create --title 'fix: x (proj#1)' --description \"$BODY\""
        assert self._fields_for(cmd) is None

    def test_command_substitution_title_is_skipped(self):
        cmd = "glab mr create --title \"$(echo hi)\" --description 'fix: x (proj#1)'"
        assert self._fields_for(cmd) is None

    def test_double_quoted_backtick_description_is_skipped(self):
        cmd = "glab mr create --title 'fix: x (proj#1)' --description \"use `foo` helper\""
        assert self._fields_for(cmd) is None

    def test_single_quoted_literal_dollar_is_validated_not_skipped(self):
        # Single-quoted values are literal: a literal '$' is real text, validated.
        title, _desc = _fields("glab mr create --title 'fix: costs $5 (proj#1)' --description 'fix: costs $5 (proj#1)'")
        assert title == "fix: costs $5 (proj#1)"

    def test_create_with_description_no_title_still_validated(self):
        # Regression: a create missing --title is still bad metadata, not skipped.
        assert self._fields_for("glab mr create --description 'x'") == ("", "x")
