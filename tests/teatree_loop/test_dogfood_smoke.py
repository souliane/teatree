"""Unit tests for the provision-smoke harness (#1308).

The harness orchestrates a fixed sequence of ``t3 <overlay> ...`` steps and
produces a categorised :class:`SmokeReport`. Tests inject a fake step
runner so the suite never shells out — the live run would take minutes
and depend on Docker / overlay infra.
"""

from collections.abc import Iterable
from dataclasses import dataclass

import pytest

from teatree.core.provision.variant import Variant
from teatree.loop.dogfood_smoke import (
    STEP_OUTCOME_KIND,
    WORKTREE_PATH_PLACEHOLDER,
    SmokeOutcomeKind,
    SmokeReport,
    SmokeStep,
    StepResult,
    default_steps,
    pick_alias_variant,
    report_summary,
    run_smoke,
    total_step_budget_seconds,
)


def _step_result(step: SmokeStep, *, returncode: int = 0, stderr: str = "", timed_out: bool = False) -> StepResult:
    return StepResult(
        step=step,
        returncode=returncode,
        stderr=stderr,
        stdout="",
        elapsed_seconds=0.01,
        timed_out=timed_out,
    )


class _ScriptedRunner:
    """Replay a scripted list of ``(returncode, stderr, timed_out)`` per step name."""

    def __init__(self, plan: dict[str, tuple[int, str, bool]]) -> None:
        self.plan = plan
        self.calls: list[str] = []

    def __call__(self, step: SmokeStep) -> StepResult:
        self.calls.append(step.name)
        rc, stderr, timed_out = self.plan.get(step.name, (0, "", False))
        return _step_result(step, returncode=rc, stderr=stderr, timed_out=timed_out)


def _all_green(steps: Iterable[SmokeStep]) -> dict[str, tuple[int, str, bool]]:
    return {step.name: (0, "", False) for step in steps}


def _fixed_worktree_path() -> str:
    """Stand in for the DB lookup in every test that is not about path binding."""
    return "/w/checkout"


class TestDefaultSteps:
    def test_sequence_order_provision_then_start_then_ready_then_teardown_then_clean(self) -> None:
        names = [step.name for step in default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")]
        assert names == [
            "workspace_ticket",
            "env_show",
            "worktree_provision",
            "workspace_provision_env_unset",
            "workspace_provision_env_set",
            "worktree_start",
            "worktree_ready",
            "worktree_teardown",
            "workspace_clean_all",
        ]

    def test_workspace_ticket_carries_fixture_url_and_variant(self) -> None:
        step = default_steps(
            overlay="testoverlay",
            fixture_ticket_url="https://github.com/souliane/teatree/issues/1308",
            variant="test-variant",
        )[0]
        assert step.name == "workspace_ticket"
        assert "https://github.com/souliane/teatree/issues/1308" in step.command
        assert "--variant" in step.command
        assert "test-variant" in step.command

    def test_workspace_ticket_omits_variant_when_unset(self) -> None:
        step = default_steps(
            overlay="testoverlay",
            fixture_ticket_url="https://x/issues/1",
        )[0]
        assert "--variant" not in step.command

    def test_step_commands_use_provided_overlay_name(self) -> None:
        steps = default_steps(overlay="customoverlay", fixture_ticket_url="https://x/issues/1")
        for step in steps:
            assert step.command[1] == "customoverlay"


class TestRunSmoke:
    def test_all_steps_pass_marks_report_pass(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        runner = _ScriptedRunner(_all_green(steps))

        report = run_smoke(steps, runner=runner, resolve_worktree_path=_fixed_worktree_path)

        assert report.passed is True
        assert report.outcome is SmokeOutcomeKind.PASS
        assert report.failing_step == ""
        assert len(report.steps) == len(steps)
        assert runner.calls == [step.name for step in steps]

    def test_orchestration_calls_each_step_in_order(self) -> None:
        steps = [
            SmokeStep(name="a", command=("a",)),
            SmokeStep(name="b", command=("b",)),
            SmokeStep(name="c", command=("c",)),
        ]
        runner = _ScriptedRunner(_all_green(steps))

        run_smoke(steps, runner=runner, resolve_worktree_path=_fixed_worktree_path)

        assert runner.calls == ["a", "b", "c"]

    def test_provision_failure_categorised_as_provision_failed(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        plan = _all_green(steps)
        plan["worktree_provision"] = (1, "dslr alias missing\n", False)
        runner = _ScriptedRunner(plan)

        report = run_smoke(steps, runner=runner, resolve_worktree_path=_fixed_worktree_path)

        assert report.outcome is SmokeOutcomeKind.PROVISION_FAILED
        assert report.failing_step == "worktree_provision"
        assert "dslr alias missing" in report.failing_step_stderr

    def test_start_failure_categorised_as_start_failed(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        plan = _all_green(steps)
        plan["worktree_start"] = (1, "docker boom", False)
        runner = _ScriptedRunner(plan)

        report = run_smoke(steps, runner=runner, resolve_worktree_path=_fixed_worktree_path)

        assert report.outcome is SmokeOutcomeKind.START_FAILED
        assert report.failing_step == "worktree_start"

    def test_ready_failure_categorised_as_ready_failed(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        plan = _all_green(steps)
        plan["worktree_ready"] = (1, "health 503", False)
        runner = _ScriptedRunner(plan)

        report = run_smoke(steps, runner=runner, resolve_worktree_path=_fixed_worktree_path)

        assert report.outcome is SmokeOutcomeKind.READY_FAILED
        assert report.failing_step == "worktree_ready"

    def test_teardown_failure_categorised_as_teardown_failed(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        plan = _all_green(steps)
        plan["worktree_teardown"] = (1, "container still up", False)
        runner = _ScriptedRunner(plan)

        report = run_smoke(steps, runner=runner, resolve_worktree_path=_fixed_worktree_path)

        assert report.outcome is SmokeOutcomeKind.TEARDOWN_FAILED
        assert report.failing_step == "worktree_teardown"

    def test_timeout_categorised_as_timeout_and_stops_sequence(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        plan = _all_green(steps)
        plan["worktree_start"] = (-1, "", True)
        runner = _ScriptedRunner(plan)

        report = run_smoke(steps, runner=runner, resolve_worktree_path=_fixed_worktree_path)

        assert report.outcome is SmokeOutcomeKind.TIMEOUT
        assert report.failing_step == "worktree_start"
        # Sequence stops at the timed-out step — subsequent steps must not run.
        assert "worktree_ready" not in runner.calls
        assert "worktree_teardown" not in runner.calls

    def test_first_failure_short_circuits_remaining_steps(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        plan = _all_green(steps)
        plan["worktree_provision"] = (1, "broken", False)
        runner = _ScriptedRunner(plan)

        run_smoke(steps, runner=runner, resolve_worktree_path=_fixed_worktree_path)

        # Steps after worktree_provision must NOT execute on failure — a
        # green teardown cannot prove the rest of the sequence, and a
        # broken provision invalidates everything that follows.
        assert "worktree_start" not in runner.calls
        assert "worktree_ready" not in runner.calls
        assert "worktree_teardown" not in runner.calls
        assert "workspace_clean_all" not in runner.calls

    def test_unknown_step_name_falls_back_to_unknown_outcome(self) -> None:
        custom = [SmokeStep(name="unmapped_step", command=("noop",))]
        runner = _ScriptedRunner({"unmapped_step": (1, "boom", False)})

        report = run_smoke(custom, runner=runner)

        assert report.outcome is SmokeOutcomeKind.UNKNOWN
        assert report.failing_step == "unmapped_step"

    def test_runner_crash_recorded_as_failure(self) -> None:
        steps = [SmokeStep(name="worktree_provision", command=("noop",))]

        def boom(step: SmokeStep) -> StepResult:
            msg = "classifier denied"
            raise RuntimeError(msg)

        report = run_smoke(steps, runner=boom, resolve_worktree_path=_fixed_worktree_path)

        assert report.outcome is SmokeOutcomeKind.PROVISION_FAILED
        assert report.failing_step == "worktree_provision"
        assert "classifier denied" in report.failing_step_stderr


class TestReportSummary:
    def test_pass_summary_includes_step_count(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        runner = _ScriptedRunner(_all_green(steps))
        report = run_smoke(steps, runner=runner, resolve_worktree_path=_fixed_worktree_path)

        assert "PASS" in report_summary(report)
        assert str(len(steps)) in report_summary(report)

    def test_failure_summary_names_step_and_outcome(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        plan = _all_green(steps)
        plan["worktree_provision"] = (1, "dslr alias missing", False)
        runner = _ScriptedRunner(plan)

        report = run_smoke(steps, runner=runner, resolve_worktree_path=_fixed_worktree_path)
        summary = report_summary(report)

        assert "provision_failed" in summary
        assert "worktree_provision" in summary
        assert "dslr alias missing" in summary


class TestStepOutcomeKindCoverage:
    @pytest.mark.parametrize(
        ("step_name", "expected_kind"),
        [
            ("workspace_ticket", SmokeOutcomeKind.PROVISION_FAILED),
            ("env_show", SmokeOutcomeKind.PROVISION_FAILED),
            ("worktree_provision", SmokeOutcomeKind.PROVISION_FAILED),
            ("worktree_start", SmokeOutcomeKind.START_FAILED),
            ("worktree_ready", SmokeOutcomeKind.READY_FAILED),
            ("worktree_teardown", SmokeOutcomeKind.TEARDOWN_FAILED),
            ("workspace_clean_all", SmokeOutcomeKind.CLEAN_FAILED),
            ("workspace_provision_env_unset", SmokeOutcomeKind.OVERLAY_RESOLUTION_FAILED),
            ("workspace_provision_env_set", SmokeOutcomeKind.PROVISION_FAILED),
        ],
    )
    def test_every_default_step_has_an_outcome_mapping(self, step_name: str, expected_kind: SmokeOutcomeKind) -> None:
        assert STEP_OUTCOME_KIND[step_name] is expected_kind

    def test_no_default_step_is_missing_from_the_outcome_table(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        assert [s.name for s in steps if s.name not in STEP_OUTCOME_KIND] == []

    def test_empty_smoke_passes_trivially(self) -> None:
        report = run_smoke([], runner=_step_result)
        assert report.passed
        assert report.steps == []


def test_smoke_report_is_dataclass_with_evidence_trail() -> None:
    """The report carries the per-step evidence trail for the DM body."""
    report = SmokeReport()
    assert report.steps == []
    assert report.outcome is SmokeOutcomeKind.PASS


class TestFailingStepStderrLookup:
    """Cover ``SmokeReport.failing_step_stderr`` iteration (#1308)."""

    def test_returns_stderr_of_named_failing_step(self) -> None:
        first = SmokeStep(name="step_a", command=("t3", "a"))
        second = SmokeStep(name="step_b", command=("t3", "b"))
        report = SmokeReport(
            outcome=SmokeOutcomeKind.PROVISION_FAILED,
            failing_step="step_b",
            steps=[
                _step_result(first, returncode=0),
                _step_result(second, returncode=1, stderr="boom on b"),
            ],
        )
        assert report.failing_step_stderr == "boom on b"

    def test_returns_empty_when_failing_step_name_does_not_match_any_result(self) -> None:
        step = SmokeStep(name="step_a", command=("t3", "a"))
        report = SmokeReport(
            outcome=SmokeOutcomeKind.UNKNOWN,
            failing_step="step_z",  # not in steps
            steps=[_step_result(step, returncode=1, stderr="boom")],
        )
        assert report.failing_step_stderr == ""

    def test_returns_empty_for_passing_report(self) -> None:
        assert SmokeReport().failing_step_stderr == ""


class TestDecodeSubprocessOutput:
    """Cover :func:`teatree.loop.dogfood_smoke._decode_subprocess_output` (#1308)."""

    def test_decodes_bytes_with_utf8(self) -> None:
        from teatree.loop.dogfood_smoke import _decode_subprocess_output  # noqa: PLC0415

        assert _decode_subprocess_output(b"hello \xe2\x9c\x93") == "hello ✓"

    def test_replaces_invalid_utf8_bytes(self) -> None:
        from teatree.loop.dogfood_smoke import _decode_subprocess_output  # noqa: PLC0415

        # ``\xff`` is not valid UTF-8 — must be replaced, not raised.
        result = _decode_subprocess_output(b"bad: \xff bytes")
        assert "bad: " in result
        assert "bytes" in result

    def test_passes_str_through_unchanged(self) -> None:
        from teatree.loop.dogfood_smoke import _decode_subprocess_output  # noqa: PLC0415

        assert _decode_subprocess_output("already text") == "already text"

    def test_returns_empty_string_for_none(self) -> None:
        from teatree.loop.dogfood_smoke import _decode_subprocess_output  # noqa: PLC0415

        assert _decode_subprocess_output(None) == ""


class TestRunT3CommandRunner:
    """Cover :func:`teatree.loop.dogfood_smoke.run_t3_command` (#1308).

    The runner is the production-mode default — tests inject fakes for
    other paths. The CI suite never shells out, so we mock the underlying
    ``run_allowed_to_fail`` and ``TimeoutExpired`` path.
    """

    def test_run_t3_command_captures_completed_process(self) -> None:
        from subprocess import CompletedProcess  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.loop.dogfood_smoke import run_t3_command  # noqa: PLC0415

        step = SmokeStep(name="workspace_ticket", command=("t3", "teatree", "workspace", "ticket"))
        fake = CompletedProcess(args=step.command, returncode=0, stdout="ok", stderr="")
        with patch("teatree.loop.dogfood_smoke.run_allowed_to_fail", return_value=fake):
            result = run_t3_command(step)

        assert result.step is step
        assert result.returncode == 0
        assert result.stdout == "ok"
        assert result.stderr == ""
        assert result.timed_out is False
        assert result.elapsed_seconds >= 0

    def test_run_t3_command_propagates_non_zero_return_code(self) -> None:
        from subprocess import CompletedProcess  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.loop.dogfood_smoke import run_t3_command  # noqa: PLC0415

        step = SmokeStep(name="worktree_provision", command=("t3", "teatree", "worktree", "provision"))
        fake = CompletedProcess(args=step.command, returncode=2, stdout="", stderr="bad config")
        with patch("teatree.loop.dogfood_smoke.run_allowed_to_fail", return_value=fake):
            result = run_t3_command(step)

        assert result.returncode == 2
        assert result.stderr == "bad config"
        assert result.timed_out is False

    def test_run_t3_command_converts_timeout_into_timed_out_result(self) -> None:
        from subprocess import TimeoutExpired  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.loop.dogfood_smoke import run_t3_command  # noqa: PLC0415

        step = SmokeStep(
            name="worktree_start",
            command=("t3", "teatree", "worktree", "start"),
            timeout_seconds=1,
        )
        # TimeoutExpired carries stdout/stderr that may be bytes — exercise both.
        exc = TimeoutExpired(cmd=step.command, timeout=1, output=b"partial\xffstdout", stderr=b"partial stderr")
        with patch("teatree.loop.dogfood_smoke.run_allowed_to_fail", side_effect=exc):
            result = run_t3_command(step)

        assert result.timed_out is True
        assert result.returncode == -1
        assert "partial stderr" in result.stderr
        assert "partial" in result.stdout
        assert result.elapsed_seconds >= 0

    def test_run_t3_command_strips_inherited_django_settings_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression: a leaked ``DJANGO_SETTINGS_MODULE`` breaks the child (#3516).

        It crashes the child's overlay-entry-point import with
        ``AppRegistryNotReady`` (a dogfood-smoke run against a process that
        already bootstrapped Django). Every other subprocess-spawning path in
        this codebase (``cli/overlay.py:_base_env()``,
        ``self_update.py:_self_db_migrate_env()``) strips the inherited var
        before shelling out to a bare ``t3`` command; this runner must too.
        """
        from subprocess import CompletedProcess  # noqa: PLC0415 -- test-local, see #3516
        from unittest.mock import patch  # noqa: PLC0415 -- test-local, see #3516

        from teatree.loop.dogfood_smoke import run_t3_command  # noqa: PLC0415 -- test-local, see #3516

        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "teatree.settings")
        monkeypatch.setenv("SOME_OTHER_VAR", "keep-me")
        step = SmokeStep(name="workspace_ticket", command=("t3", "teatree", "workspace", "ticket"))
        fake = CompletedProcess(args=step.command, returncode=0, stdout="ok", stderr="")
        with patch("teatree.loop.dogfood_smoke.run_allowed_to_fail", return_value=fake) as mock_run:
            run_t3_command(step)

        passed_env = mock_run.call_args.kwargs["env"]
        assert passed_env is not None
        assert "DJANGO_SETTINGS_MODULE" not in passed_env
        assert passed_env["SOME_OTHER_VAR"] == "keep-me"


@dataclass(frozen=True)
class _FakeConfig:
    known_variants: list[str]


class _FakeProvisioning:
    def __init__(self, aliases: dict[str, str]) -> None:
        self._aliases = aliases

    def resolve_variant(self, name: str) -> Variant:
        return Variant(name=name, canonical_tenant=self._aliases.get(name, name))


class _FakeOverlay:
    """Minimal stand-in for the two seams ``pick_alias_variant`` reads."""

    def __init__(self, *, known_variants: list[str], aliases: dict[str, str]) -> None:
        self.config = _FakeConfig(known_variants)
        self.provisioning = _FakeProvisioning(aliases)


class TestOverlayEnvMatrix:
    """The overlay-resolution class — a bare ``get_overlay()`` on a multi-overlay install."""

    def _by_name(self) -> dict[str, SmokeStep]:
        return {s.name: s for s in default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")}

    def test_workspace_env_matrix_steps_run_after_worktree_provision(self) -> None:
        names = [s.name for s in default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")]
        assert names.index("workspace_provision_env_unset") > names.index("worktree_provision")
        assert names.index("workspace_provision_env_set") > names.index("workspace_provision_env_unset")
        assert names.index("worktree_start") > names.index("workspace_provision_env_set")
        assert names.index("worktree_teardown") > names.index("worktree_ready")

    @pytest.mark.parametrize(
        "step_name",
        ["workspace_provision_env_unset", "worktree_start", "worktree_ready"],
    )
    def test_env_unset_steps_drop_the_overlay_name_var(self, step_name: str) -> None:
        step = self._by_name()[step_name]
        assert "T3_OVERLAY_NAME" in step.env_unset
        assert "T3_OVERLAY_NAME" not in step.env_overrides

    def test_env_set_step_pins_the_overlay_name_var(self) -> None:
        step = self._by_name()["workspace_provision_env_set"]
        assert step.env_overrides["T3_OVERLAY_NAME"] == "testoverlay"
        assert step.env_unset == frozenset()

    def test_env_unset_failure_categorised_as_overlay_resolution_failed(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        plan = _all_green(steps)
        plan["workspace_provision_env_unset"] = (1, "ImproperlyConfigured: Multiple overlays found (a, b)", False)

        report = run_smoke(steps, runner=_ScriptedRunner(plan), resolve_worktree_path=_fixed_worktree_path)

        assert report.outcome is SmokeOutcomeKind.OVERLAY_RESOLUTION_FAILED
        assert report.failing_step == "workspace_provision_env_unset"
        assert "Multiple overlays found" in report.failing_step_stderr


class TestTotalStepBudget:
    """The smoke reaches its caller as ONE command — its total must stay bounded."""

    def test_budget_is_the_sum_of_the_per_step_ceilings(self) -> None:
        steps = [
            SmokeStep(name="a", command=("t3", "a"), timeout_seconds=30),
            SmokeStep(name="b", command=("t3", "b"), timeout_seconds=90),
        ]
        assert total_step_budget_seconds(steps) == 120

    def test_empty_sequence_costs_nothing(self) -> None:
        assert total_step_budget_seconds([]) == 0

    def test_env_matrix_adds_only_the_two_workspace_provision_ceilings(self) -> None:
        # The acceptance's start/ready coverage rides the EXISTING steps under the
        # failure-mode env, so the matrix costs two step ceilings, not six.
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        matrix = [s for s in steps if s.name.startswith("workspace_provision_env_")]
        assert len(matrix) == 2
        assert total_step_budget_seconds(steps) - total_step_budget_seconds(matrix) == 540


class TestRunnerEnvControl:
    """``run_t3_command`` applies the step's per-step env overlay."""

    def _captured_env(self, step: SmokeStep) -> dict[str, str]:
        from subprocess import CompletedProcess  # noqa: PLC0415  # (#1308)
        from unittest.mock import patch  # noqa: PLC0415  # (#1308)

        from teatree.loop.dogfood_smoke import run_t3_command  # noqa: PLC0415  # (#1308)

        fake = CompletedProcess(args=step.command, returncode=0, stdout="", stderr="")
        with patch("teatree.loop.dogfood_smoke.run_allowed_to_fail", return_value=fake) as mock_run:
            run_t3_command(step)
        return mock_run.call_args.kwargs["env"]

    def test_env_unset_removes_inherited_var_from_child(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "leaked-from-parent")
        monkeypatch.setenv("KEEP_ME", "yes")
        step = SmokeStep(name="s", command=("t3", "x"), env_unset=frozenset({"T3_OVERLAY_NAME"}))

        env = self._captured_env(step)

        assert "T3_OVERLAY_NAME" not in env
        assert env["KEEP_ME"] == "yes"

    def test_env_overrides_set_var_in_child(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        step = SmokeStep(name="s", command=("t3", "x"), env_overrides={"T3_OVERLAY_NAME": "pinned"})

        assert self._captured_env(step)["T3_OVERLAY_NAME"] == "pinned"

    def test_env_overrides_win_over_an_inherited_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "inherited")
        step = SmokeStep(name="s", command=("t3", "x"), env_overrides={"T3_OVERLAY_NAME": "pinned"})

        assert self._captured_env(step)["T3_OVERLAY_NAME"] == "pinned"

    def test_django_settings_module_is_still_stripped_alongside_env_unset(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "teatree.settings")
        monkeypatch.setenv("T3_OVERLAY_NAME", "leaked")
        step = SmokeStep(name="s", command=("t3", "x"), env_unset=frozenset({"T3_OVERLAY_NAME"}))

        env = self._captured_env(step)

        assert "DJANGO_SETTINGS_MODULE" not in env
        assert "T3_OVERLAY_NAME" not in env


class TestPickAliasVariant:
    """Acceptance: pick a variant whose canonical tenant is NON-identity (#1308 comment 3)."""

    def test_returns_a_variant_from_the_left_side_of_the_alias_map(self) -> None:
        overlay = _FakeOverlay(
            known_variants=["plainbank", "acme-metro"],
            aliases={"acme-metro": "acme"},
        )
        assert pick_alias_variant(overlay) == "acme-metro"

    def test_returns_empty_when_every_known_variant_is_identity_mapped(self) -> None:
        overlay = _FakeOverlay(known_variants=["plainbank", "acme"], aliases={})
        assert pick_alias_variant(overlay) == ""

    def test_returns_empty_when_the_overlay_declares_no_variants(self) -> None:
        assert pick_alias_variant(_FakeOverlay(known_variants=[], aliases={})) == ""

    def test_ignores_blank_variant_names(self) -> None:
        overlay = _FakeOverlay(known_variants=["", "  "], aliases={})
        assert pick_alias_variant(overlay) == ""

    def test_a_resolve_failure_never_propagates_out_of_the_scan(self) -> None:
        class _Boom(_FakeProvisioning):
            def resolve_variant(self, name: str) -> Variant:
                msg = "overlay blew up"
                raise RuntimeError(msg)

        overlay = _FakeOverlay(known_variants=["x"], aliases={})
        overlay.provisioning = _Boom({})

        assert pick_alias_variant(overlay) == ""


class TestUncoveredAcceptance:
    """A PASS must never hide an acceptance item the run could not exercise."""

    def test_summary_names_uncovered_items_on_a_passing_run(self) -> None:
        summary = report_summary(SmokeReport(uncovered=["dslr-alias-variant"]))

        assert "PASS" in summary
        assert "uncovered" in summary
        assert "dslr-alias-variant" in summary

    def test_summary_has_no_uncovered_clause_when_everything_was_exercised(self) -> None:
        assert "uncovered" not in report_summary(SmokeReport())

    def test_uncovered_defaults_to_empty(self) -> None:
        assert SmokeReport().uncovered == []


class _RecordingRunner:
    """Capture each step's fully-resolved command, exit 0 for every step."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, step: SmokeStep) -> StepResult:
        self.commands.append(step.command)
        return _step_result(step)

    @property
    def names(self) -> list[str]:
        return [command[2] if len(command) > 2 else command[0] for command in self.commands]


class TestWorktreePathThreading:
    """Every worktree-scoped step targets the worktree ``workspace_ticket`` created."""

    #: The two steps that must NOT carry ``--path`` — one creates the worktree, one sweeps
    #: the whole workspace and takes no path at all.
    PATHLESS = frozenset({"workspace_ticket", "workspace_clean_all"})

    def _steps(self) -> list[SmokeStep]:
        return default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")

    def test_every_worktree_scoped_step_carries_the_path_placeholder(self) -> None:
        for step in self._steps():
            carries = WORKTREE_PATH_PLACEHOLDER in step.command
            assert carries is (step.name not in self.PATHLESS), step.name

    def test_placeholder_is_substituted_with_the_resolved_path(self) -> None:
        runner = _RecordingRunner()

        report = run_smoke(self._steps(), runner=runner, resolve_worktree_path=lambda: "/w/checkout")

        assert report.passed is True
        assert ("t3", "testoverlay", "env", "show", "--path", "/w/checkout") in runner.commands
        assert not [part for command in runner.commands for part in command if part == WORKTREE_PATH_PLACEHOLDER]

    def test_path_is_resolved_once_and_only_after_workspace_ticket_ran(self) -> None:
        runner = _RecordingRunner()
        resolved_after: list[int] = []

        def resolver() -> str:
            resolved_after.append(len(runner.commands))
            return "/w/checkout"

        run_smoke(self._steps(), runner=runner, resolve_worktree_path=resolver)

        assert resolved_after == [1]

    def test_unresolvable_path_fails_loud_instead_of_shelling_out_pathless(self) -> None:
        runner = _RecordingRunner()

        report = run_smoke(self._steps(), runner=runner, resolve_worktree_path=lambda: "")

        assert report.outcome is SmokeOutcomeKind.PROVISION_FAILED
        assert report.failing_step == "env_show"
        assert "worktree path" in report.failing_step_stderr
        assert len(runner.commands) == 1

    def test_absent_resolver_fails_loud_rather_than_running_the_placeholder(self) -> None:
        runner = _RecordingRunner()

        report = run_smoke(self._steps(), runner=runner)

        assert report.failing_step == "env_show"
        assert len(runner.commands) == 1

    def test_resolver_crash_is_categorised_rather_than_raised(self) -> None:
        runner = _RecordingRunner()

        def boom() -> str:
            msg = "control db unreachable"
            raise RuntimeError(msg)

        report = run_smoke(self._steps(), runner=runner, resolve_worktree_path=boom)

        assert report.failing_step == "env_show"
        assert "control db unreachable" in report.failing_step_stderr
