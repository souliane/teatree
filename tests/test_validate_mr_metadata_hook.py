"""Tests for the validate-mr-metadata PreToolUse hook (#119 Part 3).

The hook was a permanent no-op because it was gated behind
``T3_MR_VALIDATE_SCRIPT``, which is never set anywhere. The fix makes it
invoke ``t3 tool validate-mr`` (the active overlay's ``validate_pr``) BY
DEFAULT so a bad MR title/description is rejected BEFORE the push, every
time, with no opt-in. The env var remains an optional override.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts import gate_result, mr_validator, t3_invocation
from hooks.scripts.forge_api_detect import _is_existing_pr_metadata_only_edit
from hooks.scripts.gate_result import GateSkipped
from hooks.scripts.hook_router import handle_validate_mr_metadata
from hooks.scripts.mr_cli_fields import _api_field_args, extract_api_mr_fields
from teatree.config import COLD_HOOK_SETTINGS
from teatree.core.review.mr_metadata import validate_mr_metadata
from teatree.types import DEFAULT_MR_TITLE_REGEX

_ALLOWANCE_KEY = "hook_validator_timeout_seconds"


def _verdict(command: str) -> list[str] | None:
    """``validate_mr_metadata`` verdict for *command* (``None`` when gate skips)."""
    fields = router._extract_mr_fields({"tool_name": "Bash", "tool_input": {"command": command}})
    if fields is None or isinstance(fields, GateSkipped):
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
    """Extract (title, description) for *command*, asserting it IS validated."""
    result = router._extract_mr_fields({"tool_name": "Bash", "tool_input": {"command": command}})
    assert result is not None
    assert not isinstance(result, GateSkipped)
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
        assert not {"--title", "--description"} & set(argv), "the pair rides stdin, never argv"
        assert json.loads(run.call_args.kwargs["input"]) == {"title": "", "description": ""}

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


class TestUnvalidatedOutcomeIsAnnounced:
    """Every PASS-path outcome that skips validation says so — silence is reserved.

    The gate's DENY path always talked; its PASS path did not. A recognised MR
    create whose ``--description`` is an unexpanded ``$(< body.md)`` was allowed
    through with ZERO output — indistinguishable, from outside the hook, from a
    gate that swallowed the call. When the command then failed silently for its
    own unrelated reason, the mute gate is what got blamed, and hours went into
    the wrong layer.

    So the contract is: exactly two outcomes are silent — "not an MR mutation"
    and a clean PASS. Every other outcome names itself on stderr.
    """

    def _stderr_for(self, monkeypatch, capsys, command: str) -> str:
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {"tool_name": "Bash", "tool_input": {"command": command}}
        assert handle_validate_mr_metadata(data) is False
        return capsys.readouterr().err

    def test_dynamic_description_skip_names_the_field_and_says_skipped_is_not_passed(self, monkeypatch, capsys):
        # The exact shape that went mute: a file-substituted description.
        err = self._stderr_for(
            monkeypatch,
            capsys,
            "glab mr create -R acme/widget --title 'feat(x): real (proj#1)' --description \"$(< body.md)\"",
        )
        assert "MR-metadata" in err
        assert "--description" in err
        assert "SKIPPED is not PASSED" in err

    def test_dynamic_title_skip_names_the_title(self, monkeypatch, capsys):
        err = self._stderr_for(
            monkeypatch, capsys, "glab mr create --title \"$TITLE\" --description 'feat(x): real (proj#1)'"
        )
        assert "--title" in err

    def test_metadata_only_update_skip_is_announced(self, monkeypatch, capsys):
        err = self._stderr_for(monkeypatch, capsys, "glab mr update 7624 --add-label needs-review")
        assert "neither a title nor a description" in err

    def test_broken_env_opt_in_bypass_is_announced(self, monkeypatch, capsys):
        # Taking the fail-closed escape hatch must not be a quiet bypass.
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setenv("T3_MR_VALIDATE_ALLOW_BROKEN_ENV", "1")
        monkeypatch.setattr(router.shutil, "which", lambda _: None)
        assert handle_validate_mr_metadata(_glab_create("bad", "bad")) is False
        err = capsys.readouterr().err
        assert "T3_MR_VALIDATE_ALLOW_BROKEN_ENV" in err
        assert "SKIPPED is not PASSED" in err

    def test_a_clean_pass_stays_silent(self, monkeypatch, capsys):
        # The gate must not become chatty: a validated, valid MR says nothing.
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(router.subprocess, "run", return_value=ok):
            assert handle_validate_mr_metadata(_glab_create("fix: x (p#1)", "fix: x (p#1)")) is False
        assert capsys.readouterr().err == ""

    def test_a_non_mr_command_stays_silent(self, monkeypatch, capsys):
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        assert handle_validate_mr_metadata({"tool_name": "Bash", "tool_input": {"command": "ls -la"}}) is False
        assert capsys.readouterr().err == ""


class TestValidatorCrashIsNotADeny:
    """A validator that RAN but CRASHED is cannot-evaluate → warn+allow, never a deny (#1528).

    A clean validation failure (``Title is empty.``) and an uncaught traceback in
    the validator both exit non-zero. Collapsing the crash into a content deny
    hard-blocks the MR with a Python traceback as the "reason" — the lockout class
    #1528 names. The crash routes to fail-open-with-one-loud-line; the remote CI
    MR-title/description job is the backstop for genuinely non-compliant content.
    """

    def test_traceback_output_allows_with_a_loud_warn(self, monkeypatch, capsys):
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.delenv("T3_MR_VALIDATE_ALLOW_BROKEN_ENV", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        crashed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Traceback (most recent call last):\n  File ...\nKeyError: 'overlay'\n",
        )
        with patch.object(router.subprocess, "run", return_value=crashed):
            blocked = handle_validate_mr_metadata(_glab_create("fix: x (p#1)", "fix: x (p#1)"))
        assert blocked is False, "a crashing validator must not deny (fail-open-with-warn)"
        captured = capsys.readouterr()
        assert captured.out.strip() == "", "a crash must emit no deny JSON on stdout"
        err = captured.err.lower()
        assert "validator" in err, "the crash warn must name the validator"
        assert "crash" in err, "the crash warn must be one loud diagnosable line"

    def test_clean_nonzero_still_denies(self, monkeypatch, capsys):
        # The content-deny path is untouched: a concise validation message
        # (no traceback) is a genuine deny, not a crash.
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Title is empty.")
        with patch.object(router.subprocess, "run", return_value=rejected):
            blocked = handle_validate_mr_metadata(_glab_create("", ""))
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"


class TestValidatorTimeoutIsNotADeny:
    """A validator that ran out of TIME is cannot-evaluate → warn+allow, never a deny.

    The allowance was a hardcoded ``timeout=10`` while ``t3 tool validate-mr``
    takes ~13s cold on an unloaded box and 25-50s under concurrent load, so the
    subprocess ALWAYS timed out and every ``gh``/``glab`` MR body edit was denied
    — a PR body could not be corrected at all. "Too slow to evaluate" is the same
    class as "ran but crashed" (crash ≠ deny, #1528), not a policy rejection.
    """

    def _timeout_run(self, monkeypatch, allowance: int = 60):
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.delenv("T3_MR_VALIDATE_ALLOW_BROKEN_ENV", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        return patch.object(
            router.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="t3", timeout=allowance)
        )

    def test_timeout_allows_with_a_loud_warn_naming_the_timeout(self, monkeypatch, capsys):
        with self._timeout_run(monkeypatch):
            blocked = handle_validate_mr_metadata(_glab_create("fix: x (p#1)", "fix: x (p#1)"))
        assert blocked is False, "a timed-out validator must not deny (fail-open-with-warn)"
        captured = capsys.readouterr()
        assert captured.out.strip() == "", "a timeout must emit no deny JSON on stdout"
        err = captured.err.lower()
        assert "validator" in err
        assert "did not finish" in err, "the warn must name the timeout as the reason, not a rejection"
        assert str(gate_result.validator_timeout_seconds()) in captured.err, "the warn must name the allowance"
        assert "hook_validator_timeout_seconds" in captured.err, "the warn must name the knob that raises it"
        assert "invalid" not in err, "the warn must not read as a content rejection"
        assert "rejected" not in err, "the warn must not read as a content rejection"

    def test_rejection_still_denies_after_the_timeout_change(self, monkeypatch, capsys):
        # Anti-vacuity: only the CANNOT_EVALUATE path moved. A validator that RAN
        # and REJECTED keeps its teeth.
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Title is empty.")
        with patch.object(router.subprocess, "run", return_value=rejected):
            blocked = handle_validate_mr_metadata(_glab_create("", ""))
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        assert "Title is empty." in out["permissionDecisionReason"]

    def test_pass_still_allows(self, monkeypatch, capsys):
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(router.subprocess, "run", return_value=ok):
            blocked = handle_validate_mr_metadata(_glab_create("fix: x (p#1)", "fix: x (p#1)"))
        assert blocked is False
        assert capsys.readouterr().err.strip() == "", "a clean pass must be silent"


class TestValidatorTimeoutAllowanceIsConfigurable:
    """The allowance is a DB-home cold-hook budget, not a magic number in the gate."""

    def test_default_allowance_covers_the_measured_validator_cost(self):
        # ~13s cold, 25-50s under load on the reference box — the default must
        # clear the loaded range with headroom, or it rots into a false deny again.
        assert gate_result._HOOK_VALIDATOR_TIMEOUT_DEFAULT_SECONDS >= 60

    def test_configured_allowance_is_passed_to_the_subprocess(self, monkeypatch):
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        monkeypatch.setattr(mr_validator, "validator_timeout_seconds", lambda: 123)
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(router.subprocess, "run", return_value=ok) as run:
            handle_validate_mr_metadata(_glab_create("fix: x (p#1)", "fix: x (p#1)"))
        assert run.call_args.kwargs["timeout"] == 123

    def test_allowance_resolves_from_the_cold_db_budget(self, monkeypatch):
        monkeypatch.setattr(
            gate_result, "teatree_int_setting", lambda name, **kwargs: 77 if name == _ALLOWANCE_KEY else 0
        )
        assert gate_result.validator_timeout_seconds() == 77

    def test_allowance_is_a_registered_cold_hook_budget(self):
        # The no-silent-drop registry: an unregistered cold budget is dropped by
        # the TOML->DB import and silently reverts to its in-code default.
        setting = COLD_HOOK_SETTINGS[_ALLOWANCE_KEY]
        assert setting.default == gate_result._HOOK_VALIDATOR_TIMEOUT_DEFAULT_SECONDS
        assert setting.scope == ""


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


class TestDynamicDescriptionFileArgSkipsInsteadOfLyingAboutIt:
    """``--description-file "$(cat x)"`` is UNRESOLVABLE, not empty.

    The hook sees the raw command before the shell expands it, so the file-arg
    regex captures the leading fragment of the substitution (``$(cat``) as the
    filename. Reading that fails and the description used to fall through as
    ``""`` — so the gate refused the MR with "MR description is empty", a reason
    that is simply FALSE and gives the agent nothing to act on. The inline
    ``--description "$(cat x)"`` spelling of the same unresolvable body already
    skipped and said so; both spellings now agree.
    """

    def _skip(self, command: str) -> GateSkipped:
        result = router._extract_mr_fields({"tool_name": "Bash", "tool_input": {"command": command}})
        assert isinstance(result, GateSkipped), f"expected a named skip, got {result!r}"
        return result

    def test_command_substitution_file_arg_skips(self):
        reason = self._skip("glab mr create --title 'fix: t' --description-file \"$(cat /tmp/body.md)\"").reason
        assert "unexpanded shell construct" in reason

    def test_variable_file_arg_skips(self):
        reason = self._skip("glab mr create --title 'fix: t' --description-file \"$BODY\"").reason
        assert "unexpanded shell construct" in reason

    def test_the_reason_denies_the_false_empty_claim_and_names_the_fix(self):
        reason = self._skip("glab mr create --title 'fix: t' --description-file \"$(cat /tmp/b.md)\"").reason
        assert "NOT an empty description" in reason
        assert "--description-file <path>" in reason

    def test_a_literal_path_that_is_merely_missing_still_reads_empty(self, tmp_path):
        # Anti-vacuity: the skip is scoped to an UNRESOLVABLE arg. A real path
        # that simply is not there keeps its old, correct verdict.
        title, description = _fields(f"glab mr create --title 'fix: t' --description-file {tmp_path}/absent.md")
        assert (title, description) == ("fix: t", "")

    def test_a_literal_path_containing_a_dollar_in_single_quotes_is_still_read(self, tmp_path):
        # Anti-vacuity the other way: an inert (single-quoted) marker is a real
        # filename character, not a construct the shell expands.
        desc = tmp_path / "$BODY.md"
        desc.write_text("fix: real (proj#1)\n", encoding="utf-8")
        _title, description = _fields(f"glab mr create --title 'fix: t' --description-file '{desc}'")
        assert description.startswith("fix: real")


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
        assert json.loads(run.call_args.kwargs["input"]) == {"title": "bad prose", "description": "bad prose"}

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


class TestTitleOnlyUpdateSkipsRequiredSections:
    """A title-only ``glab mr update`` threads ``--sections-optional`` (#3254).

    A pure retitle touches no description, so the overlay's required-section
    completeness check (``## Configuration`` / ``## Security & privacy impact``)
    must not fire on the hook's back-filled placeholder body — the true retitle
    passes. An update that DOES set a description still validates in full.
    """

    def _argv_for(self, monkeypatch, command: str) -> list[str]:
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(router.subprocess, "run", return_value=ok) as run:
            handle_validate_mr_metadata({"tool_name": "Bash", "tool_input": {"command": command}})
        return list(run.call_args[0][0])

    def test_title_only_update_threads_sections_optional(self, monkeypatch):
        argv = self._argv_for(monkeypatch, "glab mr update 7 --title 'fix(x): rename widget (proj#1)'")
        assert "--sections-optional" in argv

    def test_description_modifying_update_does_not_skip_sections(self, monkeypatch):
        argv = self._argv_for(
            monkeypatch,
            "glab mr update 7 --title 'fix(x): t (proj#1)' --description 'fix(x): t (proj#1)\n\n## What\n- x'",
        )
        assert "--sections-optional" not in argv

    def test_create_never_skips_sections(self, monkeypatch):
        # `create` is never title-only — both fields are required, sections enforced.
        argv = self._argv_for(monkeypatch, "glab mr create --title 'fix(x): t' --description 'fix(x): t'")
        assert "--sections-optional" not in argv


class TestIssueCommandsAreNeverMrMutations:
    """``gh issue`` / ``glab issue`` commands are not MR mutations — the gate must never fire.

    Issue creation is a distinct forge operation with its own title conventions
    (free-form). Applying the MR-metadata gate to an issue create/comment would
    reject any issue title that does not match the MR conventional-commit format,
    misfiring every time an agent files a bug or feature request.

    The guard must be EXPLICIT and EARLY: matching on ``glab mr`` or ``gh api
    .../pulls`` is insufficient because those patterns could grow — the gate must
    positively exclude the entire ``gh issue`` / ``glab issue`` surface before
    any MR-pattern check runs.
    """

    def test_gh_issue_create_is_never_an_mr_mutation(self):
        cmd = "gh issue create --repo souliane/teatree --title 'chore: cleanup' --body 'context'"
        result = router._extract_mr_fields({"tool_name": "Bash", "tool_input": {"command": cmd}})
        assert result is None

    def test_glab_issue_create_is_never_an_mr_mutation(self):
        cmd = "glab issue create --repo org/repo --title 'chore: cleanup' --description 'context'"
        result = router._extract_mr_fields({"tool_name": "Bash", "tool_input": {"command": cmd}})
        assert result is None

    def test_gh_issue_comment_is_never_an_mr_mutation(self):
        cmd = "gh issue comment 42 --repo souliane/teatree --body 'follow-up note'"
        result = router._extract_mr_fields({"tool_name": "Bash", "tool_input": {"command": cmd}})
        assert result is None

    def test_glab_issue_note_is_never_an_mr_mutation(self):
        cmd = "glab issue note 42 --message 'follow-up note'"
        result = router._extract_mr_fields({"tool_name": "Bash", "tool_input": {"command": cmd}})
        assert result is None

    def test_glab_mr_create_bad_title_still_blocks(self, monkeypatch, capsys):
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Title is invalid.")
        with patch.object(router.subprocess, "run", return_value=rejected):
            blocked = handle_validate_mr_metadata(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "glab mr create --title 'bad title' --description 'bad'"},
                }
            )
        assert blocked is True


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
            skipped = router._extract_mr_fields({"tool_name": "Bash", "tool_input": {"command": cmd}})
            assert isinstance(skipped, GateSkipped)
            assert "neither a title nor a description" in skipped.reason

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
    the backstop. The skip is TYPED and carries its reason — see
    :class:`TestUnvalidatedOutcomeIsAnnounced` for the loud line it drives.
    """

    def _fields_for(self, command: str):
        return router._extract_mr_fields({"tool_name": "Bash", "tool_input": {"command": command}})

    def _skip_reason(self, command: str) -> str:
        skipped = self._fields_for(command)
        assert isinstance(skipped, GateSkipped)
        return skipped.reason

    def test_command_substitution_description_is_skipped(self):
        cmd = 'glab mr create --title \'techdebt(x): real (proj#1)\' --description "$(cat "$DESC")"'
        assert "--description" in self._skip_reason(cmd)

    def test_variable_expansion_description_is_skipped(self):
        cmd = "glab mr create --title 'fix: x (proj#1)' --description \"$BODY\""
        assert "--description" in self._skip_reason(cmd)

    def test_command_substitution_title_is_skipped(self):
        cmd = "glab mr create --title \"$(echo hi)\" --description 'fix: x (proj#1)'"
        assert "--title" in self._skip_reason(cmd)

    def test_double_quoted_backtick_description_is_skipped(self):
        cmd = "glab mr create --title 'fix: x (proj#1)' --description \"use `foo` helper\""
        assert "--description" in self._skip_reason(cmd)

    def test_single_quoted_literal_dollar_is_validated_not_skipped(self):
        # Single-quoted values are literal: a literal '$' is real text, validated.
        title, _desc = _fields("glab mr create --title 'fix: costs $5 (proj#1)' --description 'fix: costs $5 (proj#1)'")
        assert title == "fix: costs $5 (proj#1)"

    def test_create_with_description_no_title_still_validated(self):
        # Regression: a create missing --title is still bad metadata, not skipped.
        assert self._fields_for("glab mr create --description 'x'") == ("", "x")


class TestMixedQuoteDescriptionCapturedInFull:
    """A ``--description`` mixing ``'`` and ``"`` is captured whole, not truncated (#3300).

    The old non-greedy regex ended the capture at the next occurrence of the
    OPENING quote char, so a body carrying both an apostrophe (``doesn't``) and a
    double-quoted phrase truncated — the gate then validated only the leading
    fragment and rejected a compliant description for a required section present
    past the first quote. shlex yields the true argument value regardless of the
    body's internal quoting.
    """

    def test_apostrophe_and_double_quoted_phrase_body_is_captured_whole(self):
        # A single-quoted description whose body contains an apostrophe (shell-
        # escaped ``'\''``) AND a double-quoted phrase, with a required section
        # placed AFTER the first inner quote so truncation would drop it.
        desc = (
            "fix(x): mixed quotes (proj#1)\n\n"
            "## What\n"
            "GitLab'\\''s handling of a \"quoted phrase\" doesn'\\''t truncate the body.\n\n"
            "## Security & privacy impact\nnone"
        )
        cmd = f"glab mr create --title 'fix(x): mixed quotes (proj#1)' --description '{desc}'"
        _title, description = _fields(cmd)
        assert '"quoted phrase"' in description
        assert description.count("doesn't") == 1
        assert "## Security & privacy impact" in description

    def test_double_quoted_body_with_escaped_quotes_and_apostrophe_is_whole(self):
        # The mirror case: a double-quoted description whose body carries escaped
        # ``\"`` double quotes and a bare apostrophe.
        cmd = (
            "glab mr create --title 'fix(x): quoting (proj#1)' "
            '--description "fix(x): quoting (proj#1)\n\n## What\n'
            'It doesn\'t drop the \\"quoted\\" tail.\n\n## Security & privacy impact\nnone"'
        )
        _title, description = _fields(cmd)
        assert '"quoted"' in description
        assert "## Security & privacy impact" in description

    def test_equals_spelling_mixed_quote_title_is_whole(self):
        # The ``--title=<value>`` equals spelling resolves the full value too.
        cmd = "glab mr create --title='fix(x): a \"quoted\" title (proj#1)' --description 'fix(x): a (proj#1)'"
        title, _description = _fields(cmd)
        assert title == 'fix(x): a "quoted" title (proj#1)'

    def test_unparseable_command_falls_back_to_regex_capture(self):
        # An unbalanced quote ELSEWHERE in the command makes shlex raise; the gate
        # must not crash — it falls back to the regex capture of the (locally
        # balanced) --description value rather than skipping validation.
        cmd = "glab mr create --title 'fix(x): t (proj#1)' --description 'clean body' && echo \"dangling"
        title, description = _fields(cmd)
        assert title == "fix(x): t (proj#1)"
        assert description == "clean body"


class TestEmbeddedTriggerIsNotAnMrMutation:
    """The trigger phrase inside a quoted arg / heredoc body is NOT a mutation.

    The gate must fire only when ``glab mr create/update`` is the command being
    executed, not when the literal text merely appears inside another command's
    quoted argument, a ``-m``/``-F`` message, or a heredoc body (a commit
    message, a doc string, a verification script). Detection runs against the
    command with quoted spans and heredoc bodies stripped; value extraction
    still uses the original command so a real invocation is unaffected.
    """

    def _fields_for(self, command: str):
        return router._extract_mr_fields({"tool_name": "Bash", "tool_input": {"command": command}})

    def test_commit_message_embedding_is_not_a_mutation(self):
        cmd = "git commit -m 'docs: explain how glab mr create validates titles'"
        assert self._fields_for(cmd) is None

    def test_double_quoted_commit_message_embedding_is_not_a_mutation(self):
        cmd = 'git commit -m "fix: stop the gate firing on glab mr update text"'
        assert self._fields_for(cmd) is None

    def test_heredoc_body_embedding_is_not_a_mutation(self):
        cmd = "python - <<'PY'\nprint('run glab mr create --title x later')\nPY"
        assert self._fields_for(cmd) is None

    def test_other_command_quoted_title_embedding_is_not_a_mutation(self):
        cmd = 'gh issue create --title "gate over-fires on glab mr update string"'
        assert self._fields_for(cmd) is None

    def test_asking_for_help_is_not_a_mutation(self):
        # The gate refuses an empty title/description on a create, and `--help` sets
        # neither — so the one command an operator reaches for after being refused was
        # refused too, with the same message about metadata it was not setting.
        assert self._fields_for("glab mr create --help") is None
        assert self._fields_for("glab mr create -h") is None
        assert self._fields_for("glab mr update --help") is None

    def test_help_inside_a_title_is_still_a_mutation(self):
        cmd = "glab mr create --title 'fix: --help me (proj#1)' --description 'fix: --help me (proj#1)'"
        assert self._fields_for(cmd) == ("fix: --help me (proj#1)", "fix: --help me (proj#1)")

    def test_real_create_after_quoted_decoy_is_still_detected(self):
        cmd = (
            "echo 'will run glab mr create' && glab mr create --title 'fix: x (proj#1)' --description 'fix: x (proj#1)'"
        )
        assert self._fields_for(cmd) == ("fix: x (proj#1)", "fix: x (proj#1)")

    def test_real_bare_create_is_still_detected(self):
        title, _desc = _fields("glab mr create --title 'fix: x (proj#1)' --description 'fix: x (proj#1)'")
        assert title == "fix: x (proj#1)"

    def test_real_metadata_only_update_still_skipped(self):
        # The strip must not turn a genuine metadata-only update into a mutation.
        assert isinstance(self._fields_for("glab mr update 7624 --reviewer alice"), GateSkipped)


class TestValidatorRunsFromAContainerVisibleCwd:
    """The validator subprocess must never inherit the harness session directory.

    The containerized ``t3`` entry point REFUSES a working directory it cannot see
    from inside the container, so an inherited session dir makes the validator exit
    non-zero for a reason that says nothing about the MR — and this gate fails
    CLOSED, so that exit became a DENY on every ``glab mr create``/``update`` from
    such a session, whatever the metadata said.

    Patched at ``subprocess.run`` rather than at a module attribute so the assertion
    holds wherever the spawn lives — the point is the cwd the process actually gets.
    """

    @pytest.fixture(autouse=True)
    def _stub_the_prover(self):
        # The probe is a subprocess.run too, so it inflates call_count past the real spawn.
        with patch.object(t3_invocation, "container_path", return_value=None):
            yield

    def _spawn(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with patch("subprocess.run") as spawn:
            spawn.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            mr_validator.run_mr_validator(["t3", "tool", "validate-mr"], "fix: x (proj#1)", "## What\nx\n## Why\ny")
        assert spawn.call_count == 1
        return spawn.call_args

    def test_an_explicit_cwd_is_passed(self, tmp_path, monkeypatch):
        assert self._spawn(tmp_path, monkeypatch).kwargs.get("cwd") is not None

    def test_the_cwd_is_the_checkout_the_hook_lives_in(self, tmp_path, monkeypatch):
        cwd = Path(self._spawn(tmp_path, monkeypatch).kwargs["cwd"])
        assert (cwd / "hooks" / "scripts" / "mr_validator.py").is_file()

    def test_the_cwd_is_not_inherited_from_the_caller(self, tmp_path, monkeypatch):
        cwd = Path(self._spawn(tmp_path, monkeypatch).kwargs["cwd"]).resolve()
        assert cwd != tmp_path.resolve()


class TestAtFileFieldIndirectionIsResolved:
    """``--field description=@file`` is judged on the FILE; ``--raw-field`` on the literal.

    The two flags are not interchangeable, and both CLIs say so: ``-F``/``--field``
    applies a magic conversion in which a value opening with ``@`` names a file to
    read (and ``@-`` names stdin), while ``-f``/``--raw-field`` adds a static string.
    So under ``--field`` the literal ``@/tmp/body.md`` is a string the forge never
    receives -- judging it is wrong in both directions, the silent one being a
    NON-compliant body that sails through because ``@/tmp/body.md`` never looks like
    a malformed title. Under ``--raw-field`` the literal IS what GitLab stores, and
    dereferencing it would validate a file the forge never sees.

    Only a path that is ABSOLUTE after ``expanduser()`` is read: a relative one
    resolves against the HOOK process's directory, which is not the directory
    ``glab`` runs in once the command opens with a ``cd``.
    """

    _ENDPOINT = "glab api --method PUT projects/x%2Fy/merge_requests/123"
    _COMPLIANT = "fix(review): a real title\n\n## What\n\nSomething.\n"

    def _description(self, command: str) -> str:
        fields = extract_api_mr_fields(command)
        assert fields is not None
        return fields[1]

    def test_a_compliant_body_in_a_file_is_read_and_passes(self, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text(self._COMPLIANT)

        description = self._description(f"{self._ENDPOINT} --field description=@{body}")

        assert description.startswith("fix(review): a real title")
        assert "@" not in description.splitlines()[0]

    def test_a_noncompliant_body_in_a_file_is_seen_as_noncompliant(self, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text("just some prose with no conventional prefix\n")

        description = self._description(f"{self._ENDPOINT} --field description=@{body}")

        assert description.startswith("just some prose")

    def test_raw_field_keeps_the_literal_the_forge_will_actually_store(self, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text(self._COMPLIANT)

        description = self._description(f"{self._ENDPOINT} --raw-field description=@{body}")

        assert description == f"@{body}"

    def test_the_short_field_flag_reads_the_file(self, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text(self._COMPLIANT)

        description = self._description(f"{self._ENDPOINT} -F description=@{body}")

        assert description.startswith("fix(review): a real title")

    def test_the_short_raw_field_flag_keeps_the_literal_case_sensitively(self, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text(self._COMPLIANT)

        description = self._description(f"{self._ENDPOINT} -f description=@{body}")

        assert description == f"@{body}"

    def test_an_unreadable_path_keeps_the_literal_while_a_readable_one_is_read(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent.md"
        readable = tmp_path / "body.md"
        readable.write_text(self._COMPLIANT)

        assert self._description(f"{self._ENDPOINT} --field description=@{missing}") == f"@{missing}"
        assert self._description(f"{self._ENDPOINT} --field description=@{readable}").startswith("fix(review):")

    def test_a_stdin_value_is_never_read_as_a_file_in_the_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "-").write_text("a file literally named dash\n")
        readable = tmp_path / "body.md"
        readable.write_text(self._COMPLIANT)
        monkeypatch.chdir(tmp_path)

        assert self._description(f"{self._ENDPOINT} --field description=@-") == "@-"
        assert self._description(f"{self._ENDPOINT} --field description=@") == "@"
        assert self._description(f"{self._ENDPOINT} --field description=@{readable}").startswith("fix(review):")

    def test_a_relative_path_keeps_the_literal_while_the_same_file_absolute_is_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = tmp_path / "body.md"
        body.write_text(self._COMPLIANT)
        monkeypatch.chdir(tmp_path)

        assert self._description(f"{self._ENDPOINT} --field description=@body.md") == "@body.md"
        assert self._description(f"{self._ENDPOINT} --field description=@{body}").startswith("fix(review):")

    def test_a_latin_1_file_returns_the_literal_and_a_utf_8_one_is_read(self, tmp_path: Path) -> None:
        latin1 = tmp_path / "latin1.md"
        latin1.write_bytes("fix(review): caf\xe9\n".encode("latin-1"))
        utf8 = tmp_path / "body.md"
        utf8.write_text(self._COMPLIANT)

        assert self._description(f"{self._ENDPOINT} --field description=@{latin1}") == f"@{latin1}"
        assert self._description(f"{self._ENDPOINT} --field description=@{utf8}").startswith("fix(review):")

    def test_an_unexpandable_user_and_a_nul_byte_path_keep_the_literal(self, tmp_path: Path) -> None:
        utf8 = tmp_path / "body.md"
        utf8.write_text(self._COMPLIANT)
        unexpandable = "@~t3nosuchuser4242/x.md"
        nul_byte = f"@{tmp_path}/bo\x00dy.md"

        assert self._description(f"{self._ENDPOINT} --field description={unexpandable}") == unexpandable
        assert self._description(f"{self._ENDPOINT} --field description={nul_byte}") == nul_byte
        assert self._description(f"{self._ENDPOINT} --field description=@{utf8}").startswith("fix(review):")


_API_ENDPOINT = "glab api --method PUT projects/x%2Fy/merge_requests/123"
_COMPLIANT_BODY = "fix(review): a real title\n\n## What\n\nSomething.\n"
_MARKER_ENV = "T3_MR_VALIDATE_MARKER"

# The stub validator RECORDS the exact pair it judged before verdicting. Without that
# record an ALLOW and a gate that never ran are the same observation -- rc 0 with empty
# stdout -- which is the very blindness these tests exist to detect. It reads the pair
# from STDIN, the channel the real validator uses, so a body over ARG_MAX still arrives.
_VALIDATOR = (
    "import json, os, sys\n"
    "judged = json.load(sys.stdin)\n"
    "with open(os.environ['T3_MR_VALIDATE_MARKER'], 'w', encoding='utf-8') as fh:\n"
    "    json.dump(judged, fh)\n"
    "for field, text in judged.items():\n"
    "    if not text.split('\\n')[0].startswith('fix('):\n"
    "        sys.stderr.write('%s is not conventional.\\n' % field)\n"
    "        sys.exit(1)\n"
    "sys.exit(0)\n"
)

_DRIVER = (
    "import io, sys, json\n"
    "import hooks.scripts.hook_router as r\n"
    "r._HANDLERS['PreToolUse'] = [r.handle_validate_mr_metadata]\n"
    "sys.argv = ['hook_router.py', '--event', 'PreToolUse']\n"
    "sys.stdin = io.StringIO(json.dumps({payload}))\n"
    "r.main()\n"
)


def _drive(
    tmp_path: Path, command: str, *, cwd: Path | None = None, timeout: float = 60
) -> subprocess.CompletedProcess[str]:
    """Run *command* through the real ``hook_router.main`` in a subprocess."""
    validator = tmp_path / "validator.py"
    validator.write_text(_VALIDATOR)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    repo_root = Path(router.__file__).resolve().parents[2]
    env: dict[str, str] = dict(os.environ)
    env.pop("T3_MR_VALIDATE_ALLOW_BROKEN_ENV", None)
    env["HOME"] = env["USERPROFILE"] = str(home)
    env["PYTHONPATH"] = str(repo_root)
    env["T3_MR_VALIDATE_SCRIPT"] = str(validator)
    env[_MARKER_ENV] = str(tmp_path / "judged.json")
    return subprocess.run(
        [sys.executable, "-c", _DRIVER.format(payload=json.dumps(payload))],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
    )


def _judged(tmp_path: Path) -> dict[str, str] | None:
    """The title/description the validator actually judged, ``None`` when it never ran."""
    marker = tmp_path / "judged.json"
    return json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else None


class TestTheGateStillEmitsADecisionOnAnUnreadableAtFile:
    """A read that raises inside the gate is a gate that never ran, not a gate that refused.

    ``hook_router.main`` wraps each handler in ``except Exception: continue`` -- a
    broken gate fails OPEN by design. So an exception escaping the ``@filename``
    read does not surface as a refusal; it silently skips the whole MR-metadata
    gate and the ``glab api`` call proceeds unvalidated. A unit test on
    ``extract_api_mr_fields`` cannot see that difference: both a refusal and a
    skipped gate leave it returning nothing useful. Only the full chain can, and
    it reads the two apart by the exit code the router hands the harness.
    """

    def _api_put(self, path: Path | str) -> str:
        return f"{_API_ENDPOINT} --field description=@{path}"

    def test_a_latin_1_at_file_is_refused_rather_than_skipping_the_gate(self, tmp_path: Path) -> None:
        latin1 = tmp_path / "latin1.md"
        latin1.write_bytes("fix(review): caf\xe9\n".encode("latin-1"))

        result = _drive(tmp_path, self._api_put(latin1))

        assert result.returncode == 2, f"the gate never ran; stdout={result.stdout!r} stderr={result.stderr!r}"
        assert json.loads(result.stdout)["permissionDecision"] == "deny"
        assert "Traceback" not in result.stderr

    def test_a_compliant_utf_8_at_file_is_allowed_through_the_same_chain(self, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text(_COMPLIANT_BODY)

        result = _drive(tmp_path, self._api_put(body))

        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert result.stdout.strip() == ""
        assert _judged(tmp_path) == {"title": _COMPLIANT_BODY, "description": _COMPLIANT_BODY}


class TestTheGluedShortFlagIsGatedLikeTheSpacedOne:
    """`-Fdescription=x` sets the same field as `-F description=x`, so it must be judged too.

    Both CLIs accept a short flag glued to its value, and the api-field matcher required a
    space or an `=` after EVERY flag -- so every glued spelling walked past the gate. The
    silent half is worse than a mute gate: a command mixing a spaced `-F title=` with a
    glued `-Fdescription=` yielded only the title, which the back-fill then copied into the
    description, so the gate PASSED on a compliant title standing in for text the forge
    would never store.
    """

    _NONCOMPLIANT = "just some prose with no conventional prefix\n"

    def test_a_glued_short_field_naming_a_noncompliant_file_is_denied(self, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text(self._NONCOMPLIANT)

        result = _drive(tmp_path, f"{_API_ENDPOINT} -Fdescription=@{body}")

        assert result.returncode == 2, f"the gate never ran; stdout={result.stdout!r} stderr={result.stderr!r}"
        assert json.loads(result.stdout)["permissionDecision"] == "deny"
        assert _judged(tmp_path) == {"title": self._NONCOMPLIANT, "description": self._NONCOMPLIANT}

    def test_a_glued_short_field_carrying_an_inline_value_is_denied(self, tmp_path: Path) -> None:
        result = _drive(tmp_path, f"{_API_ENDPOINT} -Fdescription=JUNK")

        assert result.returncode == 2, f"the gate never ran; stdout={result.stdout!r} stderr={result.stderr!r}"
        assert json.loads(result.stdout)["permissionDecision"] == "deny"
        assert _judged(tmp_path) == {"title": "JUNK", "description": "JUNK"}

    def test_a_compliant_title_is_never_back_filled_over_a_glued_junk_description(self, tmp_path: Path) -> None:
        title = "fix(review): a real title"

        result = _drive(tmp_path, f"{_API_ENDPOINT} -F 'title={title}' -Fdescription=JUNK")

        assert _judged(tmp_path) == {"title": title, "description": "JUNK"}
        assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert json.loads(result.stdout)["permissionDecision"] == "deny"

    def test_a_glued_short_raw_field_is_judged_on_the_literal_it_stores(self, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text(_COMPLIANT_BODY)

        result = _drive(tmp_path, f"{_API_ENDPOINT} -fdescription=@{body}")

        assert result.returncode == 2, f"the gate never ran; stdout={result.stdout!r} stderr={result.stderr!r}"
        assert json.loads(result.stdout)["permissionDecision"] == "deny"
        assert _judged(tmp_path) == {"title": f"@{body}", "description": f"@{body}"}

    def test_a_glued_short_field_naming_a_compliant_file_is_read_and_allowed(self, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text(_COMPLIANT_BODY)

        result = _drive(tmp_path, f"{_API_ENDPOINT} -Fdescription=@{body}")

        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert result.stdout.strip() == ""
        assert _judged(tmp_path) == {"title": _COMPLIANT_BODY, "description": _COMPLIANT_BODY}

    def test_a_long_flag_glued_to_its_key_is_not_a_spelling_either_cli_accepts(self) -> None:
        for glued in ("--fielddescription=JUNK", "--raw-fielddescription=JUNK"):
            assert extract_api_mr_fields(f"{_API_ENDPOINT} {glued}") is None, glued

    @pytest.mark.parametrize(
        ("field_args", "expected"),
        [
            ("--field description=VAL", ("VAL", "VAL")),
            ("--field=description=VAL", ("VAL", "VAL")),
            ("--field 'description=VAL WORDS'", ("VAL WORDS", "VAL WORDS")),
            ("--field description='VAL WORDS'", ("VAL WORDS", "VAL WORDS")),
            ("--raw-field description=VAL", ("VAL", "VAL")),
            ("--raw-field='description=VAL WORDS'", ("VAL WORDS", "VAL WORDS")),
            ("-F description=VAL", ("VAL", "VAL")),
            ("-F=description=VAL", ("VAL", "VAL")),
            ("-Fdescription=VAL", ("VAL", "VAL")),
            ("-F'description=VAL WORDS'", ("VAL WORDS", "VAL WORDS")),
            ('-F"description=VAL WORDS"', ("VAL WORDS", "VAL WORDS")),
            ("-fdescription=VAL", ("VAL", "VAL")),
            ("-f'description=VAL WORDS'", ("VAL WORDS", "VAL WORDS")),
            ("--field 'title=T V' -Fdescription=VAL", ("T V", "VAL")),
            ("--fielddescription=VAL", None),
            ("--field state_event=close", None),
        ],
    )
    def test_every_spelling_of_the_field_flag_lands_on_the_documented_verdict(
        self, field_args: str, expected: tuple[str, str] | None
    ) -> None:
        assert extract_api_mr_fields(f"{_API_ENDPOINT} {field_args}") == expected


_MKFIFO = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="the platform has no FIFO to build")


class TestAMessageFileTheGateCannotReadNeverBlocksTheWholeRouter:
    """A non-regular `@file` must be REFUSED, never opened -- opening one never returns.

    `open()` on a FIFO with no writer blocks forever and `/dev/zero` never ends, so the
    read outlives the router's whole PreToolUse budget. The harness then kills the
    process, and EVERY handler in the chain is skipped -- a fail-open far wider than the
    one gate. `stat(2)` answers without opening, so it is the probe that can ask the
    question safely. Each test carries its own subprocess timeout so a regression
    surfaces as `TimeoutExpired` rather than a wedged worker.
    """

    _HANG_TIMEOUT = 20

    @_MKFIFO
    def test_an_api_field_naming_a_fifo_is_refused_without_opening_it(self, tmp_path: Path) -> None:
        fifo = tmp_path / "body.fifo"
        os.mkfifo(fifo)

        result = _drive(tmp_path, f"{_API_ENDPOINT} -F description=@{fifo}", timeout=self._HANG_TIMEOUT)

        assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert json.loads(result.stdout)["permissionDecision"] == "deny"
        assert _judged(tmp_path) == {"title": f"@{fifo}", "description": f"@{fifo}"}

    def test_an_api_field_naming_a_character_device_is_refused_rather_than_read(self, tmp_path: Path) -> None:
        result = _drive(tmp_path, f"{_API_ENDPOINT} -F description=@/dev/zero", timeout=self._HANG_TIMEOUT)

        assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert json.loads(result.stdout)["permissionDecision"] == "deny"
        assert _judged(tmp_path) == {"title": "@/dev/zero", "description": "@/dev/zero"}

    @_MKFIFO
    def test_a_description_file_naming_a_fifo_is_refused_without_opening_it(self, tmp_path: Path) -> None:
        fifo = tmp_path / "body.fifo"
        os.mkfifo(fifo)
        command = f"glab mr create --title 'fix(review): a real title' --description-file {fifo}"

        result = _drive(tmp_path, command, timeout=self._HANG_TIMEOUT)

        assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert json.loads(result.stdout)["permissionDecision"] == "deny"
        assert _judged(tmp_path) == {"title": "fix(review): a real title", "description": ""}

    def test_a_description_file_naming_a_regular_file_is_still_read_and_allowed(self, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text(_COMPLIANT_BODY)
        command = f"glab mr create --title 'fix(review): a real title' --description-file {body}"

        result = _drive(tmp_path, command, timeout=self._HANG_TIMEOUT)

        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert result.stdout.strip() == ""
        assert _judged(tmp_path) == {"title": "fix(review): a real title", "description": _COMPLIANT_BODY}


class TestAMessageFileLargerThanTheForgeAcceptsKeepsTheLiteral:
    """The cap is GitLab's own 1 MiB MR-description limit, so it can refuse no body the forge would store.

    Both sizes are LITERAL, never derived from the module's own constant: a test sized
    from the constant moves with it, so it stays green for every cap value and pins only
    that some cap exists. Written this way the pair brackets the boundary -- raising the
    cap reads the over-sized file, lowering it refuses the exact-sized one.

    Measured on this host: an argv element of exactly the limit is `E2BIG` (`ARG_MAX` is
    1048576), so the router cannot carry a cap-sized description to the validator at all,
    which is why the boundary is pinned at the parser.
    """

    _FORGE_DESCRIPTION_LIMIT = 1_048_576
    _FIRST_LINE = b"fix(review): x\n"

    def _description(self, path: Path) -> str:
        fields = extract_api_mr_fields(f"{_API_ENDPOINT} --field description=@{path}")
        assert fields is not None
        return fields[1]

    def _body_of(self, tmp_path: Path, size: int) -> Path:
        body = tmp_path / "body.md"
        body.write_bytes(self._FIRST_LINE + b"y" * (size - len(self._FIRST_LINE)))
        assert body.stat().st_size == size
        return body

    def test_a_file_one_byte_over_the_forge_limit_keeps_the_literal(self, tmp_path: Path) -> None:
        body = self._body_of(tmp_path, self._FORGE_DESCRIPTION_LIMIT + 1)

        assert self._description(body) == f"@{body}"

    def test_a_file_of_exactly_the_forge_limit_is_still_read(self, tmp_path: Path) -> None:
        body = self._body_of(tmp_path, self._FORGE_DESCRIPTION_LIMIT)

        assert self._description(body).startswith("fix(review): x")


class TestTheRefusalNamesTheAtFilenameIndirectionItJudgedAsText:
    """A refusal on a literal `@path` must say WHY the file was not read.

    The gate validates the literal whenever the indirection does not resolve -- a relative
    path, `@-`, a non-regular file, one over the cap. The refusal then quotes text the
    operator never wrote as a description, with nothing naming the `@` rule, so the only
    way to learn it is trial and error against a gate that refuses each attempt.
    """

    def _reason(self, result: subprocess.CompletedProcess[str]) -> str:
        assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        return json.loads(result.stdout)["permissionDecisionReason"]

    def test_a_relative_at_path_refusal_names_the_rule_and_the_literal_it_judged(self, tmp_path: Path) -> None:
        (tmp_path / "body.md").write_text(_COMPLIANT_BODY)

        reason = self._reason(_drive(tmp_path, f"{_API_ENDPOINT} -F description=@body.md", cwd=tmp_path))

        assert "is not conventional" in reason, reason
        assert "@body.md" in reason, reason
        assert "ABSOLUTE" in reason, reason
        assert "REGULAR" in reason, reason
        assert str(1_048_576) in reason, reason
        assert "@-" in reason, reason

    def test_a_plain_inline_value_refusal_carries_no_at_filename_note(self, tmp_path: Path) -> None:
        reason = self._reason(_drive(tmp_path, f"{_API_ENDPOINT} -F description=JUNK"))

        assert "is not conventional" in reason, reason
        assert "ABSOLUTE" not in reason, reason

    def test_a_dereferenced_at_path_is_allowed_with_no_note_at_all(self, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text(_COMPLIANT_BODY)

        result = _drive(tmp_path, f"{_API_ENDPOINT} -F description=@{body}")

        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert result.stdout.strip() == ""
        assert _judged(tmp_path) == {"title": _COMPLIANT_BODY, "description": _COMPLIANT_BODY}


class TestADescriptionTooLargeForArgvIsStillValidated:
    """A body the forge accepts must be JUDGED, never waved through for its size.

    ``ARG_MAX`` is 1048576 on the reference box and a description-only edit back-fills
    ``title=description``, so a body on argv rides it TWICE -- the exec fails ``E2BIG``
    at roughly half the forge's own 1 MiB limit. The router wraps every handler in
    ``except Exception: continue``, so that ``OSError`` surfaced as rc 0 with empty
    stdout: indistinguishable from a clean PASS, on text nobody validated.

    The marker is the load-bearing assertion. A test reading only the return code
    passes under the bug in BOTH directions -- a gate that denied and a gate that never
    ran are the same rc 0/rc 2 observation -- so only the pair the validator RECORDED
    having judged separates them.
    """

    _FORGE_DESCRIPTION_LIMIT = 1_048_576
    _NONCOMPLIANT_FIRST_LINE = "just some prose with no conventional prefix\n"

    def _body_of(self, tmp_path: Path, first_line: str, size: int) -> Path:
        body = tmp_path / "body.md"
        body.write_text(first_line + "y" * (size - len(first_line)), encoding="utf-8")
        assert body.stat().st_size == size
        return body

    def test_a_noncompliant_body_at_the_forge_limit_is_denied(self, tmp_path: Path) -> None:
        body = self._body_of(tmp_path, self._NONCOMPLIANT_FIRST_LINE, self._FORGE_DESCRIPTION_LIMIT)

        result = _drive(tmp_path, f"{_API_ENDPOINT} --field description=@{body}")

        judged = _judged(tmp_path)
        assert judged is not None, f"the validator NEVER RAN; stdout={result.stdout!r} stderr={result.stderr!r}"
        assert judged["description"].startswith(self._NONCOMPLIANT_FIRST_LINE)
        assert len(judged["description"]) == self._FORGE_DESCRIPTION_LIMIT
        assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert json.loads(result.stdout)["permissionDecision"] == "deny"

    def test_a_compliant_body_at_the_forge_limit_is_judged_and_allowed(self, tmp_path: Path) -> None:
        body = self._body_of(tmp_path, "fix(review): a real title\n", self._FORGE_DESCRIPTION_LIMIT)

        result = _drive(tmp_path, f"{_API_ENDPOINT} --field description=@{body}")

        judged = _judged(tmp_path)
        assert judged is not None, f"the validator NEVER RAN; stdout={result.stdout!r} stderr={result.stderr!r}"
        assert len(judged["description"]) == self._FORGE_DESCRIPTION_LIMIT
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert result.stdout.strip() == ""

    def test_a_noncompliant_body_well_under_argv_is_denied_too(self, tmp_path: Path) -> None:
        body = self._body_of(tmp_path, self._NONCOMPLIANT_FIRST_LINE, 520_000)

        result = _drive(tmp_path, f"{_API_ENDPOINT} --field description=@{body}")

        judged = _judged(tmp_path)
        assert judged is not None, f"the validator NEVER RAN; stdout={result.stdout!r} stderr={result.stderr!r}"
        assert len(judged["description"]) == 520_000
        assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"


class TestAnExecThatNeverStartsIsAnnouncedNotSwallowed:
    """A spawn that fails outright is CANNOT_EVALUATE — loud and allowed, never silent.

    ``run_mr_validator`` caught only ``TimeoutExpired`` and ``FileNotFoundError``, so an
    ``OSError`` (``E2BIG`` on an over-``ARG_MAX`` body, ``EACCES``, ``ENOEXEC``) escaped
    into ``hook_router.main``'s ``except Exception: continue``. The router then exited 0
    with empty stdout — the byte-identical shape of a clean PASS, which is why the
    fail-open went unnoticed: nothing anywhere said the validator had not run.
    """

    _EXEC_FAILED = OSError(7, "Argument list too long", "python3")

    def _run(self) -> object:
        with patch.object(mr_validator, "run_t3", side_effect=self._EXEC_FAILED):
            return mr_validator.run_mr_validator(["t3", "tool", "validate-mr"], "fix: x (p#1)", "body")

    def test_a_failed_exec_is_a_cannot_evaluate_marker(self) -> None:
        assert isinstance(self._run(), GateSkipped)

    def test_the_marker_names_the_exec_failure_so_the_warn_can_quote_it(self) -> None:
        result = self._run()
        assert isinstance(result, GateSkipped)
        assert "Argument list too long" in result.reason

    def test_a_failed_exec_is_never_read_as_an_absent_validator(self) -> None:
        # ``None`` is the fail-CLOSED broken-env path; an exec that failed for its own
        # reason must not be graded as "no validator exists at all".
        assert self._run() is not None

    def test_the_gate_announces_the_failed_exec_and_allows(self, monkeypatch, capsys) -> None:
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")

        with patch.object(router.subprocess, "run", side_effect=self._EXEC_FAILED):
            blocked = handle_validate_mr_metadata(_glab_create("fix: x (p#1)", "body"))

        captured = capsys.readouterr()
        assert blocked is False
        assert captured.out.strip() == ""
        assert "did NOT validate this call" in captured.err, captured.err
        assert "Argument list too long" in captured.err, captured.err


class TestEveryShellSeparatorBetweenAFieldFlagAndItsValue:
    """A TAB or a newline separates a flag from its value exactly as a space does.

    The matcher accepted only a space or an ``=``, so ``--field<TAB>description=JUNK``
    yielded no fields at all and the gate emitted NOTHING — the silent-allow class the
    glued-flag defect belonged to. Both CLIs parse it: ``glab api --field<TAB>foo=bar``
    reaches the network, so the flag and its value were tokenised as a pair.
    """

    _FLAGS = ("--field", "--raw-field", "-F", "-f")
    _SEPARATORS = ("\t", "\n")

    @pytest.mark.parametrize("flag", _FLAGS)
    @pytest.mark.parametrize("separator", _SEPARATORS)
    def test_a_whitespace_separated_field_is_still_extracted(self, flag: str, separator: str) -> None:
        command = f"{_API_ENDPOINT} {flag}{separator}description=JUNK"

        assert extract_api_mr_fields(command) == ("JUNK", "JUNK"), repr(command)

    def test_a_tab_separated_noncompliant_description_is_denied_end_to_end(self, tmp_path: Path) -> None:
        result = _drive(tmp_path, f"{_API_ENDPOINT} --field\tdescription=JUNK")

        assert _judged(tmp_path) == {"title": "JUNK", "description": "JUNK"}
        assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"


class TestAValueTheShellConcatenatesIsJudgedWhole:
    """The gate must judge the string the forge STORES, not a compliant quoted prefix of it.

    ``-F 'title=fix(x): a (u)'"JUNK"`` is ONE shell word — the forge receives
    ``fix(x): a (u)JUNK``. Matching only the quoted span reported a PASS on text that was
    never what the command set: the round-1 back-fill masquerade reached through a
    different door. A quoted KEY (``-F "description"=JUNK``) is the same word-splitting
    fact seen from the other end, and yielded no field at all.
    """

    _TITLE = "fix(x): a (u)"

    def test_a_quoted_prefix_does_not_stand_in_for_the_concatenated_value(self) -> None:
        command = f"{_API_ENDPOINT} -F 'title={self._TITLE}'\"JUNKSUFFIX\""

        assert extract_api_mr_fields(command) == (f"{self._TITLE}JUNKSUFFIX",) * 2

    def test_a_bare_prefix_concatenated_with_a_quoted_span_is_judged_whole(self) -> None:
        command = f'{_API_ENDPOINT} -F description=fix"(x): a (u)"TAIL'

        assert extract_api_mr_fields(command) == ("fix(x): a (u)TAIL",) * 2

    def test_a_quoted_key_still_names_the_field_it_sets(self) -> None:
        command = f'{_API_ENDPOINT} -F "description"=JUNK'

        assert extract_api_mr_fields(command) == ("JUNK", "JUNK")

    def test_the_gate_judges_the_concatenated_value_end_to_end(self, tmp_path: Path) -> None:
        stored = f"{self._TITLE}JUNKSUFFIX"

        result = _drive(tmp_path, f"{_API_ENDPOINT} -F 'title={self._TITLE}'\"JUNKSUFFIX\"")

        assert _judged(tmp_path) == {"title": stored, "description": stored}
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

    def test_a_quoted_key_naming_a_noncompliant_value_is_denied_end_to_end(self, tmp_path: Path) -> None:
        result = _drive(tmp_path, f'{_API_ENDPOINT} -F "description"=JUNK')

        assert _judged(tmp_path) == {"title": "JUNK", "description": "JUNK"}
        assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"


class TestBothFieldFlagMatchersAgreeOnWhatAFieldFlagLooksLike:
    """Two regexes disagreeing about the same grammar is how the glued-flag defect happened.

    ``mr_cli_fields`` extracts the metadata a field flag SETS; ``forge_api_detect`` extracts
    the KEY it names, to decide whether a write is a metadata-only edit. They spelled the
    same flag differently, so ``-Fdescription=`` was visible to one and invisible to the
    other — and a command mixing a spaced metadata field with a glued STATE field then read
    as metadata-only, exempting a state change from the gate that governs it.
    """

    _SPELLINGS = (
        "--field description=X",
        "--field=description=X",
        "--field\tdescription=X",
        "--raw-field description=X",
        "-F description=X",
        "-F=description=X",
        "-Fdescription=X",
        "-F\tdescription=X",
        "-fdescription=X",
    )

    @pytest.mark.parametrize("spelling", _SPELLINGS)
    def test_a_spelling_one_matcher_reads_is_read_by_the_other(self, spelling: str) -> None:
        command = f"glab api --method PUT projects/x%2Fy/merge_requests/123 {spelling}"

        assert _api_field_args(command), spelling
        assert _is_existing_pr_metadata_only_edit(command), spelling

    def test_a_glued_state_field_is_not_hidden_behind_a_spaced_metadata_one(self) -> None:
        # The divergence's real cost: the state field the second matcher could not see.
        command = (
            "glab api --method PUT projects/x%2Fy/merge_requests/123 --field title=fix(x): a (u) -Fstate_event=close"
        )

        assert _is_existing_pr_metadata_only_edit(command) is False
