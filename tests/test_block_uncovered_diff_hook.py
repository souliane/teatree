# test-path: cross-cutting — drives the hooks/scripts/coverage_gate.py PreToolUse gate; the
# teatree.utils.diff_coverage import is only the byte-identity drift guard (#3521), no src/teatree/ mirror.
"""Tests for the per-diff-coverage PreToolUse hook (#937, §17.6 gate 12).

Gate 12's detection (``teatree.utils.diff_coverage`` / ``t3 tool
diff-coverage``) shipped correct in #862 but was wired into ZERO
automatic enforcement points — absent from CI, pre-commit and the
``hook_router.py`` ``PreToolUse`` chain. §17.6.3 requires it to "run as
a pre-merge gate ... A PR that triggers either check is returned to
draft automatically". This gate mirrors the sibling Gate-15
(``handle_block_ai_signature``) shape: it intercepts the merge-class
mutations that move a PR toward review/merge — ``gh pr ready`` (a draft
PR being un-drafted) and a non-draft ``gh pr create`` / ``glab mr
create`` — and refuses (``deny``) when ``t3 tool diff-coverage`` reports
an uncovered new line or an unreferenced changed symbol. Reverting the
wiring (the ``_HANDLERS`` registration / the handler returning ``True``)
turns the block tests red — the anti-vacuity guarantee.
"""

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts import coverage_gate
from hooks.scripts.coverage_gate import diff_coverage_finding, repo_ships_branch, shipped_branch
from hooks.scripts.existing_artifact import _PROBE_TIMEOUT_S
from hooks.scripts.hook_budget import HOOK_CEILING_S
from hooks.scripts.hook_router import _is_merge_class_mutation, handle_block_uncovered_diff
from teatree.utils.diff_coverage import UNREFERENCED_SYMBOL_IMPORT_HINT

SHIP_BRANCH = "feat/widget"


def build_repo(root: Path, name: str, remote: str, *, branch: str = SHIP_BRANCH) -> Path:
    """A real git repo with an ``origin`` remote and *branch* checked out.

    The gate proves a measurement is in scope by resolving the push remote of
    the branch being shipped, so a test that expects the gate to REACH the
    measurement must hand it a repo that genuinely ships something. Real git
    under ``tmp_path`` per the repo's test doctrine — no faked ``.git`` dirs.
    """
    repo = root / name
    repo.mkdir(parents=True)
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "config", "user.email", "t@example.com"], check=True)
    subprocess.run([*git, "config", "user.name", "t"], check=True)
    subprocess.run([*git, "remote", "add", "origin", remote], check=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    subprocess.run([*git, "checkout", "-qb", branch], check=True)
    return repo


@pytest.fixture
def shipping_repo(tmp_path: Path) -> Path:
    """The repo a gated create command ships from — the in-scope measurement target."""
    return build_repo(
        tmp_path,
        "session-clone",
        "git@gitlab.com:my-org/my-repo.git",  # privacy-scan:allow
    )


@dataclass
class T3Measurement:
    """What the gate asked ``t3 tool`` for, if it asked at all."""

    calls: int = 0
    argv: list[str] = field(default_factory=list)
    cwd: str | None = None
    timeout: float | None = None
    open_pr_argv: list[str] = field(default_factory=list)
    open_pr_timeout: float | None = None

    @property
    def ran(self) -> bool:
        return self.calls > 0

    @property
    def repo(self) -> Path:
        return Path(self.argv[self.argv.index("--repo") + 1])


@dataclass
class MeasurementClock:
    """A monotonic clock the faked measurement advances, so "slow" costs no real time."""

    cost_s: float = 0.0
    now: float = 0.0

    def monotonic(self) -> float:
        return self.now

    def spend(self) -> None:
        self.now += self.cost_s


NO_OPEN_PR = json.dumps({"outcome": "none", "url": ""})


@contextmanager
def t3_reports(
    stdout: str,
    *,
    returncode: int = 0,
    raises: Exception | None = None,
    open_pr: str = NO_OPEN_PR,
    clock: MeasurementClock | None = None,
) -> Iterator[T3Measurement]:
    """Fake ONLY the ``t3 tool`` shellouts; let every git probe run for real.

    The gate resolves its scope with real ``git`` reads against the repo under
    test, so a blanket ``subprocess.run`` patch would swallow those too and the
    test would grade a stubbed resolver instead of the real one. ``t3`` is the
    genuine external boundary (a separate tool with its own environment) and is
    the only call faked here — both the ``diff-coverage`` measurement and the
    ``open-pr`` artifact probe the deny path runs (#4151).
    """
    real_run = subprocess.run
    measurement = T3Measurement()

    def dispatch(argv, **kwargs):
        if isinstance(argv, list) and "open-pr" in argv:
            measurement.open_pr_argv = argv
            measurement.open_pr_timeout = kwargs.get("timeout")
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=open_pr, stderr="")
        if not (isinstance(argv, list) and "diff-coverage" in argv):
            return real_run(argv, **kwargs)
        measurement.calls += 1
        measurement.argv = argv
        measurement.cwd = kwargs.get("cwd")
        measurement.timeout = kwargs.get("timeout")
        if clock is not None:
            clock.spend()
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(args=argv, returncode=returncode, stdout=stdout, stderr="")

    with patch.object(router.subprocess, "run", side_effect=dispatch):
        yield measurement


CLEAN_REPORT = json.dumps({"passes": True, "uncovered": [], "unreferenced_symbols": []})


class TestMergeClassMutationDetection:
    """The trigger surface: PR moving toward review/merge."""

    def test_gh_pr_ready_is_merge_class(self):
        assert _is_merge_class_mutation({"tool_name": "Bash", "tool_input": {"command": "gh pr ready 42"}}) is True

    def test_non_draft_gh_pr_create_is_merge_class(self):
        cmd = "gh pr create --title t --body b"
        assert _is_merge_class_mutation({"tool_name": "Bash", "tool_input": {"command": cmd}}) is True

    def test_non_draft_glab_mr_create_is_merge_class(self):
        cmd = "glab mr create --title t --description d"
        assert _is_merge_class_mutation({"tool_name": "Bash", "tool_input": {"command": cmd}}) is True

    def test_draft_pr_create_is_not_merge_class(self):
        # A draft PR is not yet under review — the gate fires when it is
        # un-drafted (gh pr ready), not at draft creation.
        cmd = "gh pr create --draft --title t --body b"
        assert _is_merge_class_mutation({"tool_name": "Bash", "tool_input": {"command": cmd}}) is False

    def test_gh_pr_ready_undo_is_not_merge_class(self):
        # `gh pr ready --undo` returns the PR TO draft — that is the gate's
        # remediation, never the thing it should block.
        cmd = "gh pr ready 42 --undo"
        assert _is_merge_class_mutation({"tool_name": "Bash", "tool_input": {"command": cmd}}) is False

    def test_unrelated_command_is_not_merge_class(self):
        assert _is_merge_class_mutation({"tool_name": "Bash", "tool_input": {"command": "ls -la"}}) is False

    def test_non_bash_tool_is_not_merge_class(self):
        assert _is_merge_class_mutation({"tool_name": "Read", "tool_input": {"file_path": "/x"}}) is False

    def test_mention_inside_quoted_argument_is_not_merge_class(self):
        # The verb is detected on the quote/heredoc-stripped skeleton: a commit
        # message (or any quoted argument) that merely MENTIONS the create verb
        # must not fire the gate against the session repo's uncommitted diff.
        cmd = 'git commit -m "docs: describe the glab mr create flow"'
        assert _is_merge_class_mutation({"tool_name": "Bash", "tool_input": {"command": cmd}}) is False

    def test_mention_inside_heredoc_body_is_not_merge_class(self):
        # A python script fed via heredoc naming the verb in a string literal
        # false-fired the gate and denied an unrelated read-only script.
        cmd = "python3 - <<'EOF'\nCMD = \"glab mr create -R o/r\"\nprint(CMD)\nEOF"
        assert _is_merge_class_mutation({"tool_name": "Bash", "tool_input": {"command": cmd}}) is False


class TestMetadataOnlyPrEditIsNotMergeClass:
    """Retitling an open PR creates nothing and merges nothing — the gate must not fire.

    The per-diff coverage gate blocks a PUSH, so every spurious fire is paid by the
    author. Several sibling gates *require* a PR's own description to be corrected
    (conventional-commit first line, ``## What`` / ``## Why``); classifying that
    correction as a merge-class write made satisfying one gate trip another. Only a
    write that changes repository state — creating a PR, merging it, closing it —
    is merge-class.
    """

    def _mutation(self, command: str) -> bool:
        return _is_merge_class_mutation({"tool_name": "Bash", "tool_input": {"command": command}})

    def test_patching_only_title_and_body_is_not_merge_class(self):
        cmd = "gh api -X PATCH repos/souliane/teatree/pulls/3887 -f title=t -f body=b"
        assert self._mutation(cmd) is False

    def test_patching_only_the_description_is_not_merge_class(self):
        cmd = "glab api -X PUT projects/12/merge_requests/77 -f description=d"
        assert self._mutation(cmd) is False

    def test_api_create_on_the_collection_endpoint_stays_merge_class(self):
        cmd = "gh api -X POST repos/souliane/teatree/pulls -f title=t -f body=b"
        assert self._mutation(cmd) is True

    def test_closing_a_pr_stays_merge_class(self):
        cmd = "gh api -X PATCH repos/souliane/teatree/pulls/3887 -f state=closed"
        assert self._mutation(cmd) is True

    def test_retargeting_the_base_branch_stays_merge_class(self):
        cmd = "gh api -X PATCH repos/souliane/teatree/pulls/3887 -f base=main"
        assert self._mutation(cmd) is True

    def test_undrafting_via_the_api_stays_merge_class(self):
        cmd = "gh api -X PATCH repos/souliane/teatree/pulls/3887 -f draft=false"
        assert self._mutation(cmd) is True


def _finding_json(*, uncovered: list[dict] | None = None, symbols: list[str] | None = None) -> str:
    return json.dumps(
        {
            "passes": False,
            "uncovered": uncovered if uncovered is not None else [{"path": "src/x.py", "lines": [3]}],
            "unreferenced_symbols": symbols or [],
        }
    )


def test_finding_row_names_the_from_import_workaround():
    # souliane/teatree#3521: the rendered symbol-finding row (consumed by
    # the deny reason) names the imports-only reading + the `from <module>
    # import <symbol>` workaround, so a false-positive on an already-covered
    # symbol is discoverable.
    finding = diff_coverage_finding(_finding_json(uncovered=[], symbols=["widget"]))
    assert finding is not None
    assert "widget" in finding
    assert "from module import symbol" in finding
    assert "import statements only" in finding


class TestBlocksUncoveredDiff:
    def test_blocks_gh_pr_ready_when_diff_coverage_fails(self, shipping_repo, monkeypatch, capsys):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {"tool_name": "Bash", "tool_input": {"command": "gh pr ready 42"}, "cwd": str(shipping_repo)}
        with t3_reports(_finding_json(), returncode=1) as measurement:
            assert handle_block_uncovered_diff(data) is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        assert "gate 12" in out["permissionDecisionReason"]
        # It shelled `t3 tool diff-coverage --json` — reusing the gate as-is.
        assert measurement.argv[:3] == ["/usr/local/bin/t3", "tool", "diff-coverage"]
        assert "--json" in measurement.argv

    def test_blocks_non_draft_pr_create_on_unreferenced_symbol(self, shipping_repo, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": f"gh pr create --head {SHIP_BRANCH} --title t --body b"},
            "cwd": str(shipping_repo),
        }
        with t3_reports(_finding_json(uncovered=[], symbols=["build_widget"]), returncode=1):
            assert handle_block_uncovered_diff(data) is True

    def test_deny_preamble_names_the_from_import_workaround(self, shipping_repo, monkeypatch, capsys):
        # souliane/teatree#3521: the deny preamble (shown even for a
        # line-only finding, symbols=[]) must not say a bare "cover it" —
        # it names the imports-only reading and the `from <module> import
        # <symbol>` workaround, since a flagged symbol may already be
        # covered via `import mod` + `mod.sym()`.
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {"tool_name": "Bash", "tool_input": {"command": "gh pr ready 42"}, "cwd": str(shipping_repo)}
        with t3_reports(_finding_json(), returncode=1):
            assert handle_block_uncovered_diff(data) is True
        reason = json.loads(capsys.readouterr().out)["permissionDecisionReason"]
        assert "from <module> import <symbol>" in reason
        assert "imports only" in reason
        assert "attribute access" in reason


class TestDenyNamesTheArtifactThatAlreadyExists:
    """#4151: a refusal must not read as "nothing happened" when the PR exists.

    The pre-push ``ensure-pr`` hook opens a PR for a branch on its first push, so
    by the time a ``gh pr create`` is denied the artifact can already be live. A
    bare deny sent the agent into a retry that collided with ``already exists``,
    and left a PR nothing was tracking. Fail-open throughout: anything the probe
    cannot answer leaves the deny exactly as it was.
    """

    CREATE = f"gh pr create --head {SHIP_BRANCH} --title t --body b"
    OPEN_PR = json.dumps({"outcome": "found", "url": "https://github.com/o/r/pull/4149"})

    def _deny_reason(self, data, capsys) -> str:
        assert handle_block_uncovered_diff(data) is True
        return json.loads(capsys.readouterr().out)["permissionDecisionReason"]

    def _create_call(self, repo: Path) -> dict:
        return {"tool_name": "Bash", "tool_input": {"command": self.CREATE}, "cwd": str(repo)}

    def test_deny_names_the_open_pr_and_calls_it_flagged_not_refused(self, shipping_repo, monkeypatch, capsys):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        with t3_reports(_finding_json(uncovered=[], symbols=["retirement_notice"]), returncode=1, open_pr=self.OPEN_PR):
            reason = self._deny_reason(self._create_call(shipping_repo), capsys)
        assert "https://github.com/o/r/pull/4149" in reason
        assert "ALREADY EXISTS" in reason
        assert "FLAGGED, not refused" in reason

    def test_probe_asks_about_the_branch_the_command_ships(self, shipping_repo, monkeypatch, capsys):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        with t3_reports(_finding_json(), returncode=1, open_pr=self.OPEN_PR) as measurement:
            self._deny_reason(self._create_call(shipping_repo), capsys)
        assert measurement.open_pr_argv[:3] == ["/usr/local/bin/t3", "tool", "open-pr"]
        assert measurement.open_pr_argv[measurement.open_pr_argv.index("--branch") + 1] == SHIP_BRANCH
        assert measurement.open_pr_argv[measurement.open_pr_argv.index("--repo") + 1] == str(shipping_repo)

    def test_no_note_when_the_branch_has_no_open_pr(self, shipping_repo, monkeypatch, capsys):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        with t3_reports(_finding_json(), returncode=1, open_pr=NO_OPEN_PR):
            reason = self._deny_reason(self._create_call(shipping_repo), capsys)
        assert "ALREADY EXISTS" not in reason

    def test_no_note_when_the_probe_cannot_answer(self, shipping_repo, monkeypatch, capsys):
        # UNKNOWN is "could not ask" (missing CLI, auth failure), never "no PR" —
        # so it adds nothing rather than asserting an artifact it did not see.
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        unknown = json.dumps({"outcome": "unknown", "url": ""})
        with t3_reports(_finding_json(), returncode=1, open_pr=unknown):
            reason = self._deny_reason(self._create_call(shipping_repo), capsys)
        assert "ALREADY EXISTS" not in reason

    def test_no_note_when_the_installed_t3_predates_the_probe(self, shipping_repo, monkeypatch, capsys):
        # The hook shells the INSTALLED `t3`, which lags this repo until the next
        # `t3 update`: `tool open-pr` is then an unknown command — an error on
        # stderr, nothing on stdout. The note is an enrichment, never a
        # dependency, so that install keeps getting exactly the deny it had.
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        with t3_reports(_finding_json(), returncode=1, open_pr=""):
            reason = self._deny_reason(self._create_call(shipping_repo), capsys)
        assert "ALREADY EXISTS" not in reason

    def test_undraft_deny_carries_no_artifact_note(self, shipping_repo, monkeypatch, capsys):
        # `gh pr ready` guards the UN-DRAFT, which genuinely did not happen; its PR
        # obviously exists, so naming it would be noise, not reconciliation.
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {"tool_name": "Bash", "tool_input": {"command": "gh pr ready 42"}, "cwd": str(shipping_repo)}
        with t3_reports(_finding_json(), returncode=1, open_pr=self.OPEN_PR) as measurement:
            reason = self._deny_reason(data, capsys)
        assert "ALREADY EXISTS" not in reason
        assert measurement.open_pr_argv == []

    def test_clean_measurement_never_probes_for_an_artifact(self, shipping_repo, monkeypatch):
        # The note rides a deny that is already terminal; an allowed create must not
        # pay a forge round trip.
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        with t3_reports(CLEAN_REPORT, open_pr=self.OPEN_PR) as measurement:
            assert handle_block_uncovered_diff(self._create_call(shipping_repo)) is False
        assert measurement.open_pr_argv == []


class TestOneCeilingSharedByMeasurementAndProbe:
    """#4305: two shellouts, one 30s PreToolUse budget — the second must see the first.

    Before this, the measurement held a fixed 30s and the probe a fixed 15s: 45s of
    timeouts in a 30s window. Overrunning does not truncate the probe, it costs the
    DECISION — the harness cancels the hook, no ``permissionDecision`` is emitted,
    and the guarded create proceeds past a gate that had already refused it.
    """

    CREATE = f"gh pr create --head {SHIP_BRANCH} --title t --body b"
    OPEN_PR = json.dumps({"outcome": "found", "url": "https://github.com/o/r/pull/4305"})

    @pytest.fixture(autouse=True)
    def _t3_on_path(self, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")

    def _deny(self, repo: Path, capsys) -> str:
        data = {"tool_name": "Bash", "tool_input": {"command": self.CREATE}, "cwd": str(repo)}
        assert handle_block_uncovered_diff(data) is True
        return json.loads(capsys.readouterr().out)["permissionDecisionReason"]

    def test_measurement_leaves_room_under_the_ceiling(self, shipping_repo, capsys):
        with t3_reports(_finding_json(), returncode=1, open_pr=self.OPEN_PR) as measurement:
            self._deny(shipping_repo, capsys)
        assert measurement.timeout is not None
        assert measurement.timeout < HOOK_CEILING_S

    def test_a_slow_measurement_shrinks_the_probe_rather_than_adding_to_it(self, shipping_repo, monkeypatch, capsys):
        clock = MeasurementClock(cost_s=25.0)
        monkeypatch.setattr(coverage_gate, "time", clock)
        with t3_reports(_finding_json(), returncode=1, open_pr=self.OPEN_PR, clock=clock) as measurement:
            reason = self._deny(shipping_repo, capsys)
        assert measurement.open_pr_timeout is not None
        assert measurement.open_pr_timeout < _PROBE_TIMEOUT_S
        assert clock.now + measurement.open_pr_timeout <= HOOK_CEILING_S
        assert "ALREADY EXISTS" in reason

    def test_a_measurement_that_spends_the_ceiling_drops_the_probe_and_still_decides(
        self, shipping_repo, monkeypatch, capsys
    ):
        # The acceptance case: the probe is what gets sacrificed, never the deny.
        clock = MeasurementClock(cost_s=float(HOOK_CEILING_S))
        monkeypatch.setattr(coverage_gate, "time", clock)
        with t3_reports(_finding_json(), returncode=1, open_pr=self.OPEN_PR, clock=clock) as measurement:
            reason = self._deny(shipping_repo, capsys)
        assert measurement.open_pr_argv == []
        assert "ALREADY EXISTS" not in reason


class TestFindingNamesImportWorkaround:
    """The deny reason names the import-only workaround, not a misleading "reference it" (#3521)."""

    def test_unreferenced_symbol_finding_names_the_import_only_workaround(self) -> None:
        finding = diff_coverage_finding(_finding_json(uncovered=[], symbols=["build_widget"]))
        assert finding is not None
        assert "import statements only" in finding
        assert "does not count as a reference" in finding
        assert "from module import symbol" in finding

    def test_import_workaround_absent_from_pure_uncovered_line_finding(self) -> None:
        finding = diff_coverage_finding(_finding_json(uncovered=[{"path": "src/x.py", "lines": [3]}], symbols=[]))
        assert finding is not None
        assert "import statements only" not in finding

    def test_hook_finding_hint_is_byte_identical_to_the_canonical_source(self) -> None:
        finding = diff_coverage_finding(_finding_json(uncovered=[], symbols=["build_widget"]))
        assert finding is not None
        assert UNREFERENCED_SYMBOL_IMPORT_HINT in finding


class TestFailsOpenOnBrokenSubprocess:
    """The documented contract: DENY only on a successfully-computed finding.

    A crash (``ModuleNotFoundError: No module named 'coverage'`` — the
    dev-only ``coverage`` dep is absent from the installed ``t3`` tool
    env), an import error, a nonzero exit with no parseable JSON, or
    malformed JSON must FAIL OPEN (return ``False``). Treating a crash as
    a coverage *finding* and denying — the #122 lockout — turns every
    ``gh pr create`` into a deny. Reverting the fix (back to
    ``if result.returncode != 0: deny``) turns these RED.
    """

    def test_fail_open_on_module_not_found_crash(self, shipping_repo, monkeypatch):
        # The exact #122 shape: coverage missing → traceback on stderr,
        # empty stdout, exit 1. The current (buggy) gate denied here.
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": f"gh pr create --head {SHIP_BRANCH} --title t --body b"},
            "cwd": str(shipping_repo),
        }
        with t3_reports("", returncode=1) as measurement:
            assert handle_block_uncovered_diff(data) is False
        assert measurement.ran  # anti-vacuity: it FAILED OPEN, it did not skip before measuring

    def test_fail_open_on_nonzero_with_unparseable_stdout(self, shipping_repo, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {"tool_name": "Bash", "tool_input": {"command": "gh pr ready 42"}, "cwd": str(shipping_repo)}
        with t3_reports("some non-json error text", returncode=2) as measurement:
            assert handle_block_uncovered_diff(data) is False
        assert measurement.ran

    def test_fail_open_on_malformed_json(self, shipping_repo, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {"tool_name": "Bash", "tool_input": {"command": "gh pr ready 42"}, "cwd": str(shipping_repo)}
        with t3_reports('{"passes": fal', returncode=1) as measurement:
            assert handle_block_uncovered_diff(data) is False
        assert measurement.ran


class TestAllowsCleanCases:
    def test_allows_gh_pr_ready_when_diff_coverage_clean(self, shipping_repo, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {"tool_name": "Bash", "tool_input": {"command": "gh pr ready 42"}, "cwd": str(shipping_repo)}
        with t3_reports(CLEAN_REPORT) as measurement:
            assert handle_block_uncovered_diff(data) is False
        assert measurement.ran

    def test_noop_when_not_a_merge_class_mutation(self):
        # `git commit` is NOT the gate's trigger (Gate 12 is pre-MERGE,
        # not pre-commit) — no t3 shellout, no block.
        data = {"tool_name": "Bash", "tool_input": {"command": "git commit -m 'x'"}}
        assert handle_block_uncovered_diff(data) is False

    def test_noop_for_draft_pr_create(self, shipping_repo, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr create --draft --title t --body b"},
            "cwd": str(shipping_repo),
        }
        with t3_reports(CLEAN_REPORT) as measurement:
            assert handle_block_uncovered_diff(data) is False
        assert not measurement.ran

    def test_fail_open_when_t3_not_on_path(self, shipping_repo, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: None)
        data = {"tool_name": "Bash", "tool_input": {"command": "gh pr ready 42"}, "cwd": str(shipping_repo)}
        assert handle_block_uncovered_diff(data) is False

    def test_fail_open_when_t3_times_out(self, shipping_repo, monkeypatch):
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {"tool_name": "Bash", "tool_input": {"command": "gh pr ready 42"}, "cwd": str(shipping_repo)}
        with t3_reports("", raises=subprocess.TimeoutExpired("t3", 30)) as measurement:
            assert handle_block_uncovered_diff(data) is False
        assert measurement.ran


class TestMeasuresTheGatedCommandsWorktree:
    """Measure the worktree the gated command targets, not the session cwd.

    Anti-vacuity: the gate keys ``t3 tool diff-coverage`` to the worktree the
    gated command TARGETS (its own leading ``cd``), never the cold hook's
    inherited session cwd.

    A cross-worktree ship — the session cwd is worktree Y, but the command ships
    worktree X via ``cd X && gh pr create`` — must run ``t3 tool diff-coverage``
    against X. Reverting the cwd-resolution fix measures Y and flags X's PR with
    Y's unrelated uncovered lines (the ``wire.py`` false-flag).
    """

    def _worktree(self, root: Path, name: str) -> Path:
        return build_repo(root, name, f"git@gitlab.com:my-org/{name}.git")  # privacy-scan:allow

    def test_measures_the_cd_target_worktree_not_the_session_cwd(self, tmp_path, monkeypatch):
        x = self._worktree(tmp_path, "worktree-x")  # the PR's worktree (cd target)
        y = self._worktree(tmp_path, "worktree-y")  # the session cwd (a DIFFERENT worktree)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": f"cd {x} && gh pr create --title t --body b"},
            "cwd": str(y),
        }
        with t3_reports(_finding_json(), returncode=1) as measurement:
            assert handle_block_uncovered_diff(data) is True

        # The gate measured X (the cd target), never the session cwd Y.
        assert measurement.repo.resolve() == x.resolve()
        assert Path(measurement.cwd).resolve() == x.resolve()
        assert str(y.resolve()) not in measurement.argv

    def test_falls_back_to_session_cwd_when_command_has_no_leading_cd(self, tmp_path, monkeypatch):
        y = self._worktree(tmp_path, "session-cwd")
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr create --title t --body b"},
            "cwd": str(y),
        }
        with t3_reports(CLEAN_REPORT) as measurement:
            assert handle_block_uncovered_diff(data) is False

        # No leading cd → the gate measures the session cwd's OWN repo (correct
        # when the command runs there), never a bare cwd-less run.
        assert Path(measurement.cwd).resolve() == y.resolve()
        assert measurement.repo.resolve() == y.resolve()


class TestScopesToThePublishedRepo:
    """A publish to repo X is never gated on uncommitted symbols in repo Y (§17.6.3).

    A cross-repo ship — ``glab mr create -R other-org/other-repo`` issued from an
    unrelated clone (the session repo, carrying its own large uncommitted diff) —
    used to measure the SESSION repo and deny the create on symbols the published
    repo never sees. When the command names an explicit target repo that is NOT
    the measured repo's own git-remote slug, the measurement is skipped entirely;
    a matching (or absent) target keeps the established enforcement.
    """

    def _repo(self, root: Path, name: str, remote: str) -> Path:
        return build_repo(root, name, remote)

    def test_cross_repo_target_skips_the_session_repo_measurement(self, tmp_path, monkeypatch):
        session_repo = self._repo(
            tmp_path,
            "session-clone",
            "git@gitlab.com:my-org/session-repo.git",  # privacy-scan:allow
        )
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "glab mr create -R other-org/other-repo --title t --description d"},
            "cwd": str(session_repo),
        }
        with t3_reports(_finding_json(), returncode=1) as measurement:
            assert handle_block_uncovered_diff(data) is False
        assert not measurement.ran

    def test_matching_target_still_enforces(self, tmp_path, monkeypatch):
        # Anti-vacuity: an explicit target that IS the measured repo (bare slug
        # vs host-qualified remote) keeps the gate enforcing.
        session_repo = self._repo(
            tmp_path,
            "session-clone",
            "git@gitlab.com:my-org/my-repo.git",  # privacy-scan:allow
        )
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "glab mr create -R my-org/my-repo --title t --description d"},
            "cwd": str(session_repo),
        }
        with t3_reports(_finding_json(), returncode=1):
            assert handle_block_uncovered_diff(data) is True

    def test_flagless_create_in_the_repo_that_ships_the_branch_enforces(self, shipping_repo, monkeypatch):
        # No explicit target, and the measured repo genuinely ships the branch:
        # the gate measures it and still DENIES an under-covered diff.
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": f"glab mr create -s {SHIP_BRANCH} --title t --description d"},
            "cwd": str(shipping_repo),
        }
        with t3_reports(_finding_json(), returncode=1):
            assert handle_block_uncovered_diff(data) is True


class TestPublishTargetComesFromTheShipNotTheCwd:
    """A ship from repo B, issued from a session sitting in repo A, measures B.

    The regression this locks: with no explicit ``-R`` the gate used to answer
    "is the measured repo the publish target?" with an unconditional ``True`` —
    "no target named, so cwd must be it". A markdown-only ship of one repo,
    issued from a session in another, was then denied on dozens of uncovered
    symbols belonging to the session's repo, which the author never touched.
    The publish target now comes from the branch being shipped.
    """

    def test_tilde_cd_ship_measures_the_shipped_repo_not_the_session_cwd(self, tmp_path, monkeypatch):
        # The recorded incident's exact shape: `cd ~/<repo> && glab mr create
        # --source-branch <b>`, hook cwd = an unrelated repo. `~` is not an
        # absolute path, so the unexpanded form was anchored on the session cwd
        # and the walk-up landed on the SESSION repo's own `.git`.
        monkeypatch.setenv("HOME", str(tmp_path))
        shipped = build_repo(tmp_path, "shipped-repo", "git@gitlab.com:my-org/shipped.git")  # privacy-scan:allow
        session = build_repo(tmp_path, "session-repo", "git@gitlab.com:my-org/session.git")  # privacy-scan:allow
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {
            "tool_name": "Bash",
            "tool_input": {
                "command": f"cd ~/shipped-repo && glab mr create -s {SHIP_BRANCH} --title t --description d"
            },
            "cwd": str(session),
        }
        # The finding would belong to the SESSION repo; measuring `shipped` is
        # what makes the verdict ALLOW rather than a deny on foreign symbols.
        with t3_reports(CLEAN_REPORT) as measurement:
            assert handle_block_uncovered_diff(data) is False
        assert measurement.repo.resolve() == shipped.resolve()

    def test_repo_that_does_not_ship_the_branch_is_skipped_not_measured(self, tmp_path, monkeypatch):
        # The measured repo has no such branch, so its diff is a different
        # repo's work — skip rather than deny on foreign symbols.
        session = build_repo(tmp_path, "session-repo", "git@gitlab.com:my-org/session.git")  # privacy-scan:allow
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "glab mr create -s fix/lives-in-another-repo --title t --description d"},
            "cwd": str(session),
        }
        with t3_reports(_finding_json(), returncode=1) as measurement:
            assert handle_block_uncovered_diff(data) is False
        assert not measurement.ran

    def test_skip_reason_is_reported_not_silent(self, tmp_path, monkeypatch, capsys):
        session = build_repo(tmp_path, "session-repo", "git@gitlab.com:my-org/session.git")  # privacy-scan:allow
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "glab mr create -s fix/lives-in-another-repo --title t --description d"},
            "cwd": str(session),
        }
        with t3_reports(CLEAN_REPORT):
            handle_block_uncovered_diff(data)
        err = capsys.readouterr().err
        assert "coverage gate 12 skipped" in err
        assert "-R <owner>/<repo>" in err

    def test_unresolvable_cd_target_skips_instead_of_walking_up_to_the_session_repo(self, tmp_path, monkeypatch):
        # An unexpanded `$VAR` cd target resolves to no directory. Anchoring it
        # on the session cwd and walking UP finds the SESSION repo — a
        # confident wrong answer. Refuse to guess.
        session = build_repo(tmp_path, "session-repo", "git@gitlab.com:my-org/session.git")  # privacy-scan:allow
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "cd $WORKTREE && gh pr create --title t --body b"},
            "cwd": str(session),
        }
        with t3_reports(_finding_json(), returncode=1) as measurement:
            assert handle_block_uncovered_diff(data) is False
        assert not measurement.ran

    def test_fork_push_remote_still_enforces(self, tmp_path, monkeypatch):
        # Anti-vacuity for the fork workflow: the branch pushes to a FORK
        # remote while the MR targets upstream. The repo still ships the
        # branch, so the gate keeps measuring — a fix that made the gate stop
        # firing here would be a removal, not a fix.
        repo = build_repo(tmp_path, "fork-clone", "git@github.com:upstream-org/app.git")  # privacy-scan:allow
        git = ["git", "-C", str(repo)]
        subprocess.run([*git, "remote", "add", "fork", "git@github.com:me/app.git"], check=True)
        subprocess.run([*git, "config", f"branch.{SHIP_BRANCH}.pushRemote", "fork"], check=True)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": f"gh pr create --head {SHIP_BRANCH} --title t --body b"},
            "cwd": str(repo),
        }
        with t3_reports(_finding_json(), returncode=1):
            assert handle_block_uncovered_diff(data) is True

    def test_detached_head_with_no_named_branch_is_skipped(self, tmp_path, monkeypatch):
        repo = build_repo(tmp_path, "detached", "git@gitlab.com:my-org/detached.git")  # privacy-scan:allow
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--detach"], check=True)  # noqa: S607 — real git under tmp_path
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr create --title t --body b"},
            "cwd": str(repo),
        }
        with t3_reports(_finding_json(), returncode=1) as measurement:
            assert handle_block_uncovered_diff(data) is False
        assert not measurement.ran


class TestShippedBranchParsing:
    """The branch a create command ships, read as exact shlex tokens."""

    def test_glab_source_branch_long_flag(self):
        assert shipped_branch("glab mr create --source-branch feat/x --title t") == "feat/x"

    def test_glab_source_branch_short_flag(self):
        assert shipped_branch("glab mr create -s feat/x --title t") == "feat/x"

    def test_gh_head_flag(self):
        assert shipped_branch("gh pr create --head feat/x --title t --body b") == "feat/x"

    def test_gh_cross_fork_head_keeps_only_the_branch(self):
        # `<user>:<branch>` names the head REPO plus the ref; only the ref is a branch.
        assert shipped_branch("gh pr create --head someone:feat/x --title t --body b") == "feat/x"

    def test_glab_target_branch_is_not_the_shipped_branch(self):
        # `-b`/`--target-branch` is where the MR merges INTO, never the ship.
        assert shipped_branch("glab mr create -b main --title t --description d") is None

    def test_no_branch_flag_defers_to_the_repos_own_head(self):
        assert shipped_branch("glab mr create --title t --description d") is None

    def test_gh_pr_ready_names_no_branch(self):
        assert shipped_branch("gh pr ready 42") is None


class TestRepoShipsBranch:
    """The proof that a measured repo is the working tree the ship comes from."""

    def test_true_for_a_local_branch_with_a_push_remote(self, shipping_repo):
        assert repo_ships_branch(shipping_repo, SHIP_BRANCH) is True

    def test_falls_back_to_the_repos_checked_out_branch(self, shipping_repo):
        assert repo_ships_branch(shipping_repo, None) is True

    def test_false_for_a_branch_that_lives_elsewhere(self, shipping_repo):
        assert repo_ships_branch(shipping_repo, "fix/somewhere-else") is False

    def test_false_when_the_repo_has_no_push_destination(self, tmp_path):
        repo = build_repo(tmp_path, "remoteless", "git@gitlab.com:my-org/x.git")  # privacy-scan:allow
        subprocess.run(["git", "-C", str(repo), "remote", "remove", "origin"], check=True)  # noqa: S607 — real git under tmp_path
        assert repo_ships_branch(repo, SHIP_BRANCH) is False

    def test_false_for_a_directory_that_is_not_a_repo(self, tmp_path):
        assert repo_ships_branch(tmp_path, SHIP_BRANCH) is False


class TestRegisteredInPreToolUseChain:
    """Anti-vacuity: the handler must be WIRED, not just defined.

    Reverting the wiring (removing the handler from
    ``_HANDLERS['PreToolUse']``) turns this red — the exact false-
    completion surface #937 closes (a gate that exists but never fires).
    """

    def test_handler_is_registered_in_pretooluse(self):
        assert handle_block_uncovered_diff in router._HANDLERS["PreToolUse"]
