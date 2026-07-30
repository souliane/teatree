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
from hooks.scripts import gate_result, mr_validator
from hooks.scripts.hook_router import handle_validate_mr_metadata
from teatree.config import COLD_HOOK_SETTINGS
from teatree.core.review.mr_metadata import validate_mr_metadata
from teatree.types import DEFAULT_MR_TITLE_REGEX

_ALLOWANCE_KEY = "hook_validator_timeout_seconds"


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
        assert self._fields_for("glab mr update 7624 --reviewer alice") is None
