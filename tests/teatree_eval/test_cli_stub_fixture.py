"""The inert ``cli_stubs`` clean-room fixture, end to end.

Stubs resolve on PATH and exit 0, and the field flows through the loader/model
into the runner's child ``PATH``.

The behaviour these pins protect is the H2 harness fix: a single-action probe
whose correct ``t3``/``gh``/``glab`` command errors in the sandbox wanders into a
``max_turns`` cap-taint. The stub makes that command succeed so the agent stops —
without touching any matcher. These tests exercise the plumbing deterministically
(no model run): the stub is a real executable on a real PATH, and the runner's
``_resolve_eval_target`` prepends the stub dir to the env it hands the SDK.
"""

import os
import subprocess
from pathlib import Path

import pytest

from teatree.eval.api_runner import ApiInProcessRunner
from teatree.eval.cli_stub_fixture import KNOWN_CLI_STUBS, prepend_to_path, provision_cli_stubs
from teatree.eval.loader import EvalSpecError, load_eval_yaml
from teatree.eval.models import EvalSpec, Matcher


def _run_stub(bindir: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run *argv* with *bindir* first on PATH (so the stub, not any real CLI, resolves)."""
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(argv, env=env, capture_output=True, text=True, check=False)


class TestStubExecutables:
    def test_every_known_stub_resolves_and_exits_zero(self, tmp_path: Path) -> None:
        with provision_cli_stubs(sorted(KNOWN_CLI_STUBS)) as bindir:
            for name in KNOWN_CLI_STUBS:
                stub = bindir / name
                assert stub.is_file()
                assert os.access(stub, os.X_OK), f"{name} stub is not executable"
                result = _run_stub(bindir, [name, "--help"])
                assert result.returncode == 0, f"{name} --help exited {result.returncode}: {result.stderr}"

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["t3", "teatree", "notify", "send", "hi https://x/1", "--idempotency-key", "k"], "DM queued"),
            (["t3", "default", "notify", "dm", "hi"], "DM queued"),
            (["t3", "teatree", "lifecycle", "record-e2e-run", "42", "--posted-url", "https://x/1"], "recorded e2e run"),
            (["t3", "teatree", "e2e", "post-test-plan", "--manifest", "m.json"], "test plan posted"),
            (["t3", "teatree", "review", "record", "--verdict", "merge_safe"], "recorded verdict"),
            (["t3", "teatree", "review-request", "check", "512"], "review-requestable"),
            (["t3", "slack", "react", "C1", "1.5", "eyes"], "reaction added"),
        ],
    )
    def test_t3_stub_prints_a_success_line_per_verb_family(
        self, tmp_path: Path, argv: list[str], expected: str
    ) -> None:
        with provision_cli_stubs(["t3"]) as bindir:
            result = _run_stub(bindir, argv)
        assert result.returncode == 0
        assert expected in result.stdout

    @pytest.mark.parametrize(
        ("argv", "needle"),
        [
            (["gh", "pr", "diff", "1"], "diff --git"),
            (["gh", "pr", "view", "1"], "PR #1"),
            (["glab", "mr", "diff", "1"], "diff --git"),
            (["glab", "mr", "view", "1"], "MR !1"),
        ],
    )
    def test_forge_stub_prints_static_diff_or_summary(self, tmp_path: Path, argv: list[str], needle: str) -> None:
        with provision_cli_stubs([argv[0]]) as bindir:
            result = _run_stub(bindir, argv)
        assert result.returncode == 0
        assert needle in result.stdout

    def test_unknown_verb_still_exits_zero(self, tmp_path: Path) -> None:
        # A stray discovery call must NOT error the agent back into a wander.
        with provision_cli_stubs(["t3"]) as bindir:
            result = _run_stub(bindir, ["t3", "some", "unrecognised", "verb"])
        assert result.returncode == 0

    def test_bindir_is_removed_on_context_exit(self) -> None:
        with provision_cli_stubs(["t3"]) as bindir:
            captured = bindir
            assert captured.is_dir()
        assert not captured.exists()

    def test_unknown_stub_name_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown cli_stubs"), provision_cli_stubs(["not-a-cli"]):
            pass


class TestPrependToPath:
    def test_prepends_bindir_highest_priority(self) -> None:
        env = {"PATH": "/usr/bin"}
        out = prepend_to_path(env, Path("/stubs/bin"))
        assert out["PATH"] == f"/stubs/bin{os.pathsep}/usr/bin"

    def test_does_not_mutate_the_input_env(self) -> None:
        env = {"PATH": "/usr/bin"}
        prepend_to_path(env, Path("/stubs/bin"))
        assert env["PATH"] == "/usr/bin"

    def test_handles_missing_path(self) -> None:
        out = prepend_to_path({}, Path("/stubs/bin"))
        assert out["PATH"] == "/stubs/bin"


class TestLoaderParsesCliStubs:
    def _write(self, tmp_path: Path, body: str) -> Path:
        spec = tmp_path / "spec.yaml"
        spec.write_text(body, encoding="utf-8")
        return spec

    def test_absent_defaults_to_empty(self, tmp_path: Path) -> None:
        spec = self._write(
            tmp_path,
            "- name: s\n  scenario: x\n  agent_path: a.md\n"
            '  expect:\n    - tool_call: Bash\n      args.command: contains "git"\n',
        )
        (tmp_path / "a.md").write_text("# a\n\nbody\n", encoding="utf-8")
        loaded = load_eval_yaml(spec)
        assert loaded[0].cli_stubs == ()

    def test_declared_list_parses(self, tmp_path: Path) -> None:
        spec = self._write(
            tmp_path,
            "- name: s\n  scenario: x\n  agent_path: a.md\n  cli_stubs: [t3, gh]\n"
            '  expect:\n    - tool_call: Bash\n      args.command: contains "git"\n',
        )
        (tmp_path / "a.md").write_text("# a\n\nbody\n", encoding="utf-8")
        loaded = load_eval_yaml(spec)
        assert loaded[0].cli_stubs == ("t3", "gh")

    def test_unknown_name_is_a_spec_error(self, tmp_path: Path) -> None:
        spec = self._write(
            tmp_path,
            "- name: s\n  scenario: x\n  agent_path: a.md\n  cli_stubs: [nope]\n"
            '  expect:\n    - tool_call: Bash\n      args.command: contains "git"\n',
        )
        with pytest.raises(EvalSpecError, match="unknown cli_stubs"):
            load_eval_yaml(spec)

    def test_empty_list_is_a_spec_error(self, tmp_path: Path) -> None:
        spec = self._write(
            tmp_path,
            "- name: s\n  scenario: x\n  agent_path: a.md\n  cli_stubs: []\n"
            '  expect:\n    - tool_call: Bash\n      args.command: contains "git"\n',
        )
        with pytest.raises(EvalSpecError, match="cli_stubs"):
            load_eval_yaml(spec)


def _spec(tmp_path: Path, *, cli_stubs: tuple[str, ...] = (), fixture: str = "") -> EvalSpec:
    agent = tmp_path / "agent.md"
    agent.write_text("# fake skill\n\nbody\n", encoding="utf-8")
    return EvalSpec(
        name="probe",
        scenario="single-action probe",
        agent_path=str(agent),
        prompt="Run the one command.",
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="t3"),),
        source_path=tmp_path / "spec.yaml",
        model="haiku",
        cli_stubs=cli_stubs,
        fixture=fixture,
    )


class TestResolveEvalTargetWiresPath:
    def test_stub_dir_prepended_to_env_path_when_declared(self, tmp_path: Path) -> None:
        runner = ApiInProcessRunner(workspace=tmp_path)
        spec = _spec(tmp_path, cli_stubs=("t3",))
        with runner._resolve_eval_target(spec) as (_workspace, _cwd, env):
            first = env["PATH"].split(os.pathsep)[0]
            assert (Path(first) / "t3").is_file()
            assert os.access(Path(first) / "t3", os.X_OK)

    def test_path_untouched_when_no_cli_stubs(self, tmp_path: Path) -> None:
        runner = ApiInProcessRunner(workspace=tmp_path)
        spec = _spec(tmp_path)
        base_path = os.environ.get("PATH", "")
        with runner._resolve_eval_target(spec) as (_workspace, _cwd, env):
            assert env["PATH"] == base_path

    def test_composes_with_git_repo_fixture(self, tmp_path: Path) -> None:
        # cli_stubs is a SEPARATE lever from `fixture`; a scenario may declare both.
        runner = ApiInProcessRunner(workspace=tmp_path)
        spec = _spec(tmp_path, cli_stubs=("t3",), fixture="git_repo")
        with runner._resolve_eval_target(spec) as (workspace, cwd, env):
            assert (workspace / ".git").is_dir()  # the git_repo fixture provisioned
            assert str(workspace) == cwd
            first = env["PATH"].split(os.pathsep)[0]
            assert (Path(first) / "t3").is_file()  # AND the stub is on PATH
