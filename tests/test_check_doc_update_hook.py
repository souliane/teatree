"""Tests for the deterministic doc-update gate (souliane/teatree#1461).

The hook fails when staged changes contain a HIGH-CONFIDENCE trigger
(new top-level ``t3`` command, new ``SKILL.md``, new ``Ticket.State``
enum value, new ``LoopLease`` name) without a matching README/BLUEPRINT
diff in the same commit.

The hook is silent on a no-trigger diff and never judges soft cases —
those belong to the skill prose in ``/t3:ship`` § Documentation Discipline.
"""

import pytest

from scripts.hooks.check_doc_update import (
    Finding,
    detect_loop_lease_added,
    detect_new_skill_md,
    detect_ticket_state_added,
    detect_top_level_command_added,
    find_missing_docs,
    main,
)

_DIFF_NEW_TYPER_COMMAND = """\
diff --git a/src/teatree/cli/__init__.py b/src/teatree/cli/__init__.py
--- a/src/teatree/cli/__init__.py
+++ b/src/teatree/cli/__init__.py
@@ -120,0 +121 @@ app.add_typer(setup_app, name="setup")
+app.add_typer(newcmd_app, name="newcmd")
"""

_DIFF_NEW_DECORATED_COMMAND = """\
diff --git a/src/teatree/cli/__init__.py b/src/teatree/cli/__init__.py
--- a/src/teatree/cli/__init__.py
+++ b/src/teatree/cli/__init__.py
@@ -112,0 +113 @@ app.command()(_info.info)
+app.command()(_info.brandnew)
"""

_DIFF_REMOVED_COMMAND = """\
diff --git a/src/teatree/cli/__init__.py b/src/teatree/cli/__init__.py
--- a/src/teatree/cli/__init__.py
+++ b/src/teatree/cli/__init__.py
@@ -120,1 +120,0 @@
-app.add_typer(retired_app, name="retired")
"""

_DIFF_TICKET_STATE_ADDED = """\
diff --git a/src/teatree/core/models/ticket.py b/src/teatree/core/models/ticket.py
--- a/src/teatree/core/models/ticket.py
+++ b/src/teatree/core/models/ticket.py
@@ -47,0 +48 @@ class State(models.TextChoices):
+        ARCHIVED = "archived", "Archived"
"""

_DIFF_TICKET_NON_STATE_LINE = """\
diff --git a/src/teatree/core/models/ticket.py b/src/teatree/core/models/ticket.py
--- a/src/teatree/core/models/ticket.py
+++ b/src/teatree/core/models/ticket.py
@@ -200,0 +201 @@ def some_method(self):
+        helper = self._compute()
"""

_DIFF_LOOP_LEASE_ADDED = """\
diff --git a/src/teatree/loop/tick.py b/src/teatree/loop/tick.py
--- a/src/teatree/loop/tick.py
+++ b/src/teatree/loop/tick.py
@@ -85,0 +86 @@ def acquire_other(owner):
+    LoopLease.objects.acquire("loop-doc-scanner", owner=owner, lease_seconds=60)
"""

_DIFF_INTERNAL_REFACTOR = """\
diff --git a/src/teatree/core/util.py b/src/teatree/core/util.py
--- a/src/teatree/core/util.py
+++ b/src/teatree/core/util.py
@@ -10,0 +11,2 @@
+def _private_helper(x):
+    return x + 1
"""

_DIFF_LOOP_LEASE_PATTERN_IN_HOOK_SCRIPT = """\
diff --git a/scripts/hooks/check_doc_update.py b/scripts/hooks/check_doc_update.py
--- a/scripts/hooks/check_doc_update.py
+++ b/scripts/hooks/check_doc_update.py
@@ -30,0 +31 @@
+_LOOP_LEASE_RE = re.compile(r'LoopLease\\.objects\\.acquire\\(\\s*["\\']loop-foo["\\']')
"""

_DIFF_LOOP_LEASE_IN_TESTS = """\
diff --git a/tests/test_check_doc_update_hook.py b/tests/test_check_doc_update_hook.py
--- a/tests/test_check_doc_update_hook.py
+++ b/tests/test_check_doc_update_hook.py
@@ -45,0 +46 @@
+_TEST_DIFF = "LoopLease.objects.acquire(\\"loop-new-row\\")"
"""


class TestDetectTopLevelCommandAdded:
    def test_detects_add_typer(self) -> None:
        assert detect_top_level_command_added(_DIFF_NEW_TYPER_COMMAND) is True

    def test_detects_command_decorator(self) -> None:
        assert detect_top_level_command_added(_DIFF_NEW_DECORATED_COMMAND) is True

    def test_ignores_removed_command(self) -> None:
        assert detect_top_level_command_added(_DIFF_REMOVED_COMMAND) is False

    def test_no_trigger_on_unrelated_diff(self) -> None:
        assert detect_top_level_command_added(_DIFF_INTERNAL_REFACTOR) is False


class TestDetectTicketStateAdded:
    def test_detects_textchoice_value(self) -> None:
        assert detect_ticket_state_added(_DIFF_TICKET_STATE_ADDED) is True

    def test_skips_non_textchoice_change_in_ticket_file(self) -> None:
        assert detect_ticket_state_added(_DIFF_TICKET_NON_STATE_LINE) is False

    def test_skips_unrelated_file(self) -> None:
        assert detect_ticket_state_added(_DIFF_LOOP_LEASE_ADDED) is False


class TestDetectLoopLeaseAdded:
    def test_detects_acquire_call(self) -> None:
        assert detect_loop_lease_added(_DIFF_LOOP_LEASE_ADDED) is True

    def test_no_match_on_unrelated_diff(self) -> None:
        assert detect_loop_lease_added(_DIFF_INTERNAL_REFACTOR) is False

    def test_ignores_hook_script_self_reference(self) -> None:
        assert detect_loop_lease_added(_DIFF_LOOP_LEASE_PATTERN_IN_HOOK_SCRIPT) is False

    def test_ignores_test_file_fixtures(self) -> None:
        assert detect_loop_lease_added(_DIFF_LOOP_LEASE_IN_TESTS) is False


class TestDetectNewSkillMd:
    def test_detects_added_skill_md(self) -> None:
        files = ["plugins/t3/skills/doc-discipline/SKILL.md", "src/teatree/cli/__init__.py"]
        added = ["plugins/t3/skills/doc-discipline/SKILL.md"]
        assert detect_new_skill_md(files, added) is True

    def test_skips_edited_existing_skill(self) -> None:
        files = ["plugins/t3/skills/ship/SKILL.md"]
        added: list[str] = []
        assert detect_new_skill_md(files, added) is False

    def test_skips_non_skill_files(self) -> None:
        files = ["src/teatree/cli/__init__.py"]
        added = ["src/teatree/cli/__init__.py"]
        assert detect_new_skill_md(files, added) is False


class TestFindMissingDocs:
    def test_no_triggers_returns_empty(self) -> None:
        findings = find_missing_docs(
            diff=_DIFF_INTERNAL_REFACTOR,
            staged_files=["src/teatree/core/util.py"],
            added_files=[],
        )
        assert findings == []

    def test_new_command_without_readme_diff_returns_finding(self) -> None:
        findings = find_missing_docs(
            diff=_DIFF_NEW_TYPER_COMMAND,
            staged_files=["src/teatree/cli/__init__.py"],
            added_files=[],
        )
        assert len(findings) == 1
        assert findings[0].required_doc == "README.md"
        assert "command" in findings[0].trigger.lower()

    def test_new_command_with_readme_diff_returns_empty(self) -> None:
        findings = find_missing_docs(
            diff=_DIFF_NEW_TYPER_COMMAND,
            staged_files=["src/teatree/cli/__init__.py", "README.md"],
            added_files=[],
        )
        assert findings == []

    def test_new_state_without_blueprint_diff_returns_finding(self) -> None:
        findings = find_missing_docs(
            diff=_DIFF_TICKET_STATE_ADDED,
            staged_files=["src/teatree/core/models/ticket.py"],
            added_files=[],
        )
        assert len(findings) == 1
        assert findings[0].required_doc == "BLUEPRINT.md"
        assert "state" in findings[0].trigger.lower()

    def test_new_state_with_blueprint_diff_returns_empty(self) -> None:
        findings = find_missing_docs(
            diff=_DIFF_TICKET_STATE_ADDED,
            staged_files=["src/teatree/core/models/ticket.py", "BLUEPRINT.md"],
            added_files=[],
        )
        assert findings == []

    def test_new_loop_lease_without_blueprint_returns_finding(self) -> None:
        findings = find_missing_docs(
            diff=_DIFF_LOOP_LEASE_ADDED,
            staged_files=["src/teatree/loop/tick.py"],
            added_files=[],
        )
        assert any(f.required_doc == "BLUEPRINT.md" and "looplease" in f.trigger.lower() for f in findings)

    def test_new_skill_md_without_readme_returns_finding(self) -> None:
        findings = find_missing_docs(
            diff="",
            staged_files=["plugins/t3/skills/doc-discipline/SKILL.md"],
            added_files=["plugins/t3/skills/doc-discipline/SKILL.md"],
        )
        assert any(f.required_doc == "README.md" and "skill" in f.trigger.lower() for f in findings)

    def test_new_skill_md_with_readme_returns_empty(self) -> None:
        findings = find_missing_docs(
            diff="",
            staged_files=["plugins/t3/skills/doc-discipline/SKILL.md", "README.md"],
            added_files=["plugins/t3/skills/doc-discipline/SKILL.md"],
        )
        assert findings == []

    def test_finding_message_names_trigger_and_doc(self) -> None:
        findings = find_missing_docs(
            diff=_DIFF_NEW_TYPER_COMMAND,
            staged_files=["src/teatree/cli/__init__.py"],
            added_files=[],
        )
        assert len(findings) == 1
        assert isinstance(findings[0], Finding)
        message = findings[0].message
        assert "README.md" in message
        assert findings[0].trigger.lower() in message.lower()


class TestMain:
    def test_passes_when_no_staged_files(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scripts.hooks.check_doc_update as mod  # noqa: PLC0415

        monkeypatch.setattr(mod, "_staged_diff", lambda: "")
        monkeypatch.setattr(mod, "_staged_files", list)
        monkeypatch.setattr(mod, "_added_files", list)
        assert main() == 0

    def test_passes_when_trigger_paired_with_doc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scripts.hooks.check_doc_update as mod  # noqa: PLC0415

        monkeypatch.setattr(mod, "_staged_diff", lambda: _DIFF_NEW_TYPER_COMMAND)
        monkeypatch.setattr(mod, "_staged_files", lambda: ["src/teatree/cli/__init__.py", "README.md"])
        monkeypatch.setattr(mod, "_added_files", list)
        assert main() == 0

    def test_fails_when_trigger_without_doc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scripts.hooks.check_doc_update as mod  # noqa: PLC0415

        monkeypatch.setattr(mod, "_staged_diff", lambda: _DIFF_TICKET_STATE_ADDED)
        monkeypatch.setattr(mod, "_staged_files", lambda: ["src/teatree/core/models/ticket.py"])
        monkeypatch.setattr(mod, "_added_files", list)
        assert main() == 1

    def test_passes_on_internal_refactor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scripts.hooks.check_doc_update as mod  # noqa: PLC0415

        monkeypatch.setattr(mod, "_staged_diff", lambda: _DIFF_INTERNAL_REFACTOR)
        monkeypatch.setattr(mod, "_staged_files", lambda: ["src/teatree/core/util.py"])
        monkeypatch.setattr(mod, "_added_files", list)
        assert main() == 0


class TestSubprocessWrappers:
    def test_staged_diff_returns_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess  # noqa: PLC0415

        import scripts.hooks.check_doc_update as mod  # noqa: PLC0415

        def _fake_run(_cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=_cmd, returncode=0, stdout="diff content", stderr="")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        assert mod._staged_diff() == "diff content"

    def test_staged_files_splits_lines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess  # noqa: PLC0415

        import scripts.hooks.check_doc_update as mod  # noqa: PLC0415

        def _fake_run(_cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=_cmd, returncode=0, stdout="a.py\nb.py\n\n", stderr="")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        assert mod._staged_files() == ["a.py", "b.py"]

    def test_added_files_splits_lines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess  # noqa: PLC0415

        import scripts.hooks.check_doc_update as mod  # noqa: PLC0415

        def _fake_run(_cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=_cmd, returncode=0, stdout="new.py\n", stderr="")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        assert mod._added_files() == ["new.py"]


class TestGitFailureFailsLoud:
    """Fix #9: a non-zero `git diff --cached` must crash the gate, not pass silently.

    The old wrappers used check=False, so a git failure returned '' and main()
    early-exited 0 — every doc-update trigger silently skipped (fake-green). The
    wrappers now raise CalledProcessError on a non-zero git exit.
    """

    def _fail_run(self, returncode: int = 128, stderr: str = "fatal: corrupt index"):
        import subprocess  # noqa: PLC0415

        def _run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=cmd, returncode=returncode, stdout="", stderr=stderr)

        return _run

    def test_staged_diff_raises_on_git_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess  # noqa: PLC0415

        import scripts.hooks.check_doc_update as mod  # noqa: PLC0415

        monkeypatch.setattr(subprocess, "run", self._fail_run())
        with pytest.raises(subprocess.CalledProcessError):
            mod._staged_diff()

    def test_staged_files_raises_on_git_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess  # noqa: PLC0415

        import scripts.hooks.check_doc_update as mod  # noqa: PLC0415

        monkeypatch.setattr(subprocess, "run", self._fail_run())
        with pytest.raises(subprocess.CalledProcessError):
            mod._staged_files()

    def test_added_files_raises_on_git_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess  # noqa: PLC0415

        import scripts.hooks.check_doc_update as mod  # noqa: PLC0415

        monkeypatch.setattr(subprocess, "run", self._fail_run())
        with pytest.raises(subprocess.CalledProcessError):
            mod._added_files()

    def test_main_propagates_git_failure_instead_of_exiting_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The end-to-end fail-loud proof: a failing `git diff --cached` must NOT
        # let main() return 0 (which would silently skip every trigger).
        import subprocess  # noqa: PLC0415

        import scripts.hooks.check_doc_update as mod  # noqa: PLC0415

        monkeypatch.setattr(subprocess, "run", self._fail_run())
        with pytest.raises(subprocess.CalledProcessError):
            mod.main()
