"""The Dependabot SBOM sync follow-up: its contract with ci.yml, and its steps run for real (#4907).

The workflow holds a write credential and acts on a branch a bot opened, so the
static half pins what must never drift: it fires only for failed Dependabot uv
runs on this repo, downloads the artifact ci.yml's ``sbom`` job really uploads,
never checks out or installs the PR's tree, and pushes without force.

The functional half executes each step's ``run`` script with bash against real
git — a bare remote, a main-branch checkout, a Dependabot branch — because a
shell step nobody has run is a step nobody has tested.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_SYNC = _WORKFLOWS / "dependabot-sbom-sync.yml"
_CI = _WORKFLOWS / "ci.yml"
_DEPENDABOT = _REPO_ROOT / ".github" / "dependabot.yml"
_SBOM = _REPO_ROOT / "dist" / "sbom.json"
_DRIFT_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "sbom_drift.py"
_BASH = shutil.which("bash") or "/bin/bash"
_GIT = shutil.which("git") or "/usr/bin/git"

_JOB = "dependabot-sbom-sync"
_FRESH = "Skip a superseded head or a moved base"
_DRIFT = "Classify the SBOM drift"
_PROBE = "Verify TEATREE_GH_TOKEN before the push consumes it"
_PUSH = "Push the regenerated SBOM onto the Dependabot branch"
_VERIFY = "Verify the push by re-reading the branch"
_SBOM_INPUTS = ("uv.lock", "pyproject.toml", "dist/sbom.json", "scripts/hooks/generate_sbom.sh")
_LOAD_BEARING_CI_CHECKS = {"sbom", "test (3.13)", "uv-audit"}
_BRANCH = "dependabot/uv/python-deps-0123456789"
_TOKEN = "a-sync-credential"


def _load(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _workflow() -> dict[str, Any]:
    return _load(_SYNC)


def _trigger() -> dict[str, Any]:
    workflow = _workflow()
    on = workflow.get("on", workflow.get(True))
    return cast("dict[str, Any]", on["workflow_run"])


def _job() -> dict[str, Any]:
    return cast("dict[str, Any]", _workflow()["jobs"][_JOB])


def _steps() -> list[dict[str, Any]]:
    return [step for step in _job()["steps"] if isinstance(step, dict)]


def _step(name: str) -> dict[str, Any]:
    return next(step for step in _steps() if step.get("name") == name)


def _index(name: str) -> int:
    return next(index for index, step in enumerate(_steps()) if step.get("name") == name)


def _runs() -> list[str]:
    return [str(step["run"]) for step in _steps() if "run" in step]


def _ci_jobs() -> dict[str, Any]:
    return cast("dict[str, Any]", _load(_CI)["jobs"])


class TestTrigger:
    def test_follows_the_ci_workflow_by_its_exact_name(self) -> None:
        assert _trigger()["workflows"] == [_load(_CI)["name"]]

    def test_fires_on_completed_runs_of_dependabot_uv_branches_only(self) -> None:
        assert _trigger()["types"] == ["completed"]
        assert _trigger()["branches"] == ["dependabot/uv/**"]
        ecosystems = [update["package-ecosystem"] for update in _load(_DEPENDABOT)["updates"]]
        assert "uv" in ecosystems, "the branch filter names the uv ecosystem's dependabot/uv/ prefix"

    @pytest.mark.parametrize(
        "gate",
        [
            "github.event.workflow_run.conclusion == 'failure'",
            "github.event.workflow_run.event == 'pull_request'",
            "github.event.workflow_run.actor.login == 'dependabot[bot]'",
            "github.event.workflow_run.head_repository.full_name == github.repository",
        ],
    )
    def test_the_job_runs_only_for_a_failed_dependabot_run_on_this_repo(self, gate: str) -> None:
        assert gate in " ".join(str(_job()["if"]).split())

    def test_the_job_never_borrows_a_load_bearing_check_name(self) -> None:
        ci_names = set(_ci_jobs()) | {str(job.get("name", "")) for job in _ci_jobs().values()}
        ours = {_JOB, str(_job()["name"])}
        assert not ours & (ci_names | _LOAD_BEARING_CI_CHECKS)


class TestArtifactContract:
    def test_downloads_the_artifact_ci_sbom_job_uploads(self) -> None:
        upload = next(s for s in _ci_jobs()["sbom"]["steps"] if "upload-artifact" in str(s.get("uses", "")))
        download = next(s for s in _steps() if "download-artifact" in str(s.get("uses", "")))
        assert download["with"]["name"] == upload["with"]["name"]
        assert download["with"]["run-id"] == "${{ github.event.workflow_run.id }}"

    def test_the_upload_survives_the_failing_diff(self) -> None:
        upload = next(s for s in _ci_jobs()["sbom"]["steps"] if "upload-artifact" in str(s.get("uses", "")))
        assert upload.get("if") == "always()"
        assert upload["with"]["path"] == "dist/sbom.json"


class TestTrustBoundary:
    def test_the_workflow_token_is_read_only(self) -> None:
        assert _workflow()["permissions"] == {"contents": "read", "actions": "read"}

    def test_checks_out_only_the_default_branch_without_persisting_a_credential(self) -> None:
        checkouts = [s for s in _steps() if "actions/checkout" in str(s.get("uses", ""))]
        assert len(checkouts) == 1
        inputs = checkouts[0].get("with", {})
        assert inputs.get("persist-credentials") is False
        assert "ref" not in inputs
        assert "repository" not in inputs

    def test_never_materialises_the_pr_tree(self) -> None:
        offenders = [run for run in _runs() if re.search(r"\bgit\s+(checkout|switch|worktree|reset|restore)\b", run)]
        assert not offenders

    def test_installs_and_executes_nothing_from_the_pr(self) -> None:
        offenders = [run for run in _runs() if re.search(r"(?<![\w./-])(uv|uvx|pip3?|poetry)\s+\w", run)]
        assert not offenders

    def test_workflow_run_fields_reach_scripts_only_through_env(self) -> None:
        assert not [run for run in _runs() if "${{" in run]
        env = _job()["env"]
        assert env["HEAD_SHA"] == "${{ github.event.workflow_run.head_sha }}"
        assert env["HEAD_BRANCH"] == "${{ github.event.workflow_run.head_branch }}"


class TestPushContract:
    def test_classifies_before_anything_consumes_the_credential(self) -> None:
        assert _index(_FRESH) < _index(_DRIFT) < _index(_PROBE) < _index(_PUSH) < _index(_VERIFY)

    def test_only_a_version_only_verdict_reaches_the_credential(self) -> None:
        assert _step(_DRIFT)["if"] == "steps.fresh.outputs.proceed == 'true'"
        for name in (_PROBE, _PUSH, _VERIFY):
            assert _step(name)["if"] == "steps.drift.outputs.verdict == 'version-only'"

    def test_pushes_with_the_pat_so_ci_re_fires(self) -> None:
        assert _step(_PUSH)["env"]["TEATREE_GH_TOKEN"] == "${{ secrets.TEATREE_GH_TOKEN }}"
        assert _step(_PROBE)["env"]["GH_TOKEN"] == "${{ secrets.TEATREE_GH_TOKEN }}"

    def test_never_forces(self) -> None:
        push = str(_step(_PUSH)["run"])
        assert "git push" in push
        assert not re.search(r"git push[^\n]*(--force|\s-f\b|[\s\"']\+)", push)

    def test_commits_under_a_noreply_identity(self) -> None:
        env = _step(_PUSH)["env"]
        assert env["GIT_AUTHOR_EMAIL"].endswith("@users.noreply.github.com")
        assert env["GIT_COMMITTER_EMAIL"].endswith("@users.noreply.github.com")

    @pytest.mark.parametrize("path", _SBOM_INPUTS)
    def test_the_base_guard_covers_every_sbom_input(self, path: str) -> None:
        assert path in str(_step(_FRESH)["run"])


def _git(cwd: Path, *args: str, env: dict[str, str]) -> str:
    return subprocess.run([_GIT, *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _read_outputs(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    lines = path.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def _bumped_sbom() -> bytes:
    bom = json.loads(_SBOM.read_bytes())
    component = bom["components"][0]
    old = component["version"]
    component["version"] = "999.0.0"
    component["purl"] = component["purl"].replace(f"@{old}", "@999.0.0")
    component["description"] = component["description"].replace(f"=={old}", "==999.0.0")
    return (json.dumps(bom, indent=2) + "\n").encode()


def _shrunk_sbom() -> bytes:
    bom = json.loads(_SBOM.read_bytes())
    bom["components"].pop()
    return (json.dumps(bom, indent=2) + "\n").encode()


class Sandbox:
    """A bare remote, the runner's main checkout, and a Dependabot branch CI tested."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.temp = root / "runner-temp"
        (self.temp / "regenerated").mkdir(parents=True)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        self.gh_log = root / "gh-argv.log"
        gh = bin_dir / "gh"
        gh.write_text(f'#!/bin/sh\necho "$@" >> "{self.gh_log}"\nexit "${{GH_STUB_EXIT:-0}}"\n', encoding="utf-8")
        gh.chmod(0o755)
        self.env = {
            "PATH": os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
            "HOME": str(root),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        self.remote = root / "remote.git"
        _git(root, "init", "-q", "--bare", "-b", "main", str(self.remote), env=self.env)
        self.seed = root / "seed"
        _git(root, "clone", "-q", str(self.remote), str(self.seed), env=self.env)
        _git(self.seed, "checkout", "-q", "-b", "main", env=self.env)
        self._write(
            self.seed,
            {
                "uv.lock": "lock v1\n",
                "pyproject.toml": "[project]\n",
                "README.md": "readme\n",
                "dist/sbom.json": _SBOM.read_text(encoding="utf-8"),
                "scripts/hooks/generate_sbom.sh": "#!/bin/sh\n",
                "scripts/ci/sbom_drift.py": _DRIFT_SCRIPT.read_text(encoding="utf-8"),
            },
        )
        self._commit_and_push(self.seed, "main", "seed")
        _git(self.seed, "push", "-q", "origin", f"main:refs/heads/{_BRANCH}", env=self.env)
        self.head_sha = self.commit_on(_BRANCH, {"uv.lock": "lock v2\n"})
        self.checkout = root / "checkout"
        _git(root, "clone", "-q", str(self.remote), str(self.checkout), env=self.env)

    @staticmethod
    def _write(cwd: Path, files: dict[str, str]) -> None:
        for relative, content in files.items():
            target = cwd / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def _commit_and_push(self, cwd: Path, branch: str, message: str) -> str:
        _git(cwd, "add", "-A", env=self.env)
        _git(cwd, "commit", "-q", "-m", message, env=self.env)
        _git(cwd, "push", "-q", "origin", f"HEAD:refs/heads/{branch}", env=self.env)
        return _git(cwd, "rev-parse", "HEAD", env=self.env)

    def commit_on(self, branch: str, files: dict[str, str]) -> str:
        _git(self.seed, "fetch", "-q", "origin", env=self.env)
        _git(self.seed, "checkout", "-q", "-B", branch, f"origin/{branch}", env=self.env)
        self._write(self.seed, files)
        return self._commit_and_push(self.seed, branch, f"edit {', '.join(files)}")

    def remote_tip(self) -> str:
        return _git(self.root, "--git-dir", str(self.remote), "rev-parse", f"refs/heads/{_BRANCH}", env=self.env)

    def regenerated(self, content: bytes) -> Path:
        artifact = self.temp / "regenerated" / "sbom.json"
        artifact.write_bytes(content)
        return artifact

    def run_step(self, name: str, **extra: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        step = _step(name)
        output = self.temp / f"output-{name.replace(' ', '-')}"
        literal = {k: str(v) for k, v in (step.get("env") or {}).items() if "${{" not in str(v)}
        env = {
            **self.env,
            **literal,
            "HEAD_SHA": self.head_sha,
            "HEAD_BRANCH": _BRANCH,
            "DEFAULT_BRANCH": "main",
            "CI_RUN_ID": "36779982287",
            "RUNNER_TEMP": str(self.temp),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_REPOSITORY": "owner/repo",
            **extra,
        }
        script = self.temp / "step.sh"
        script.write_text(str(step["run"]), encoding="utf-8")
        completed = subprocess.run(
            [_BASH, "--noprofile", "--norc", "-eo", "pipefail", str(script)],
            cwd=self.checkout,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return completed, _read_outputs(output)

    def sync(self) -> dict[str, str]:
        """Run the steps a version-only drift takes, asserting each one succeeds."""
        outputs: dict[str, str] = {}
        for name in (_FRESH, _DRIFT, _PROBE, _PUSH):
            completed, produced = self.run_step(name, GH_TOKEN=_TOKEN, TEATREE_GH_TOKEN=_TOKEN)
            assert completed.returncode == 0, f"{name}: {completed.stderr}"
            outputs.update(produced)
        return outputs


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


class TestSyncsAVersionOnlyDrift:
    def test_lands_the_artifact_bytes_on_top_of_the_tested_head(self, sandbox: Sandbox) -> None:
        artifact = sandbox.regenerated(_bumped_sbom())
        outputs = sandbox.sync()
        assert outputs["proceed"] == "true"
        assert outputs["verdict"] == "version-only"
        tip = sandbox.remote_tip()
        assert tip == outputs["commit"]
        git_dir = ("--git-dir", str(sandbox.remote))
        assert _git(sandbox.root, *git_dir, "rev-parse", f"{tip}^", env=sandbox.env) == sandbox.head_sha
        changed = _git(sandbox.root, *git_dir, "diff", "--name-only", sandbox.head_sha, tip, env=sandbox.env)
        assert changed == "dist/sbom.json"
        shown = subprocess.run(
            [_GIT, *git_dir, "show", f"{tip}:dist/sbom.json"], env=sandbox.env, capture_output=True, check=True
        ).stdout
        assert shown == artifact.read_bytes()
        email = _git(sandbox.root, *git_dir, "log", "-1", "--format=%ae %ce", tip, env=sandbox.env)
        noreply = "github-actions[bot]@users.noreply.github.com"
        assert email == f"{noreply} {noreply}"

    def test_the_re_read_confirms_the_push(self, sandbox: Sandbox) -> None:
        sandbox.regenerated(_bumped_sbom())
        outputs = sandbox.sync()
        completed, _ = sandbox.run_step(_VERIFY, PUSHED=outputs["commit"])
        assert completed.returncode == 0, completed.stderr
        assert "::notice::" in completed.stdout

    def test_the_runner_checkout_stays_on_the_default_branch(self, sandbox: Sandbox) -> None:
        sandbox.regenerated(_bumped_sbom())
        sandbox.sync()
        assert (sandbox.checkout / "uv.lock").read_text(encoding="utf-8") == "lock v1\n"
        assert _git(sandbox.checkout, "status", "--porcelain", env=sandbox.env) == ""

    def test_the_credential_never_reaches_argv(self, sandbox: Sandbox) -> None:
        sandbox.regenerated(_bumped_sbom())
        sandbox.sync()
        assert _TOKEN not in sandbox.gh_log.read_text(encoding="utf-8")
        assert _TOKEN not in (sandbox.checkout / ".git" / "config").read_text(encoding="utf-8")


class TestSkipsWhatItCannotProve:
    def test_a_head_that_moved_after_the_run_is_superseded(self, sandbox: Sandbox) -> None:
        sandbox.commit_on(_BRANCH, {"uv.lock": "lock v3\n"})
        completed, outputs = sandbox.run_step(_FRESH)
        assert (completed.returncode, outputs) == (0, {"proceed": "false"})
        assert "::notice::" in completed.stdout

    def test_a_deleted_branch_is_superseded(self, sandbox: Sandbox) -> None:
        _git(sandbox.seed, "push", "-q", "origin", f":refs/heads/{_BRANCH}", env=sandbox.env)
        completed, outputs = sandbox.run_step(_FRESH)
        assert (completed.returncode, outputs) == (0, {"proceed": "false"})

    @pytest.mark.parametrize("path", _SBOM_INPUTS)
    def test_a_base_that_moved_an_sbom_input_is_skipped(self, sandbox: Sandbox, path: str) -> None:
        sandbox.commit_on("main", {path: "moved on main\n"})
        completed, outputs = sandbox.run_step(_FRESH)
        assert (completed.returncode, outputs) == (0, {"proceed": "false"})
        assert "::warning::" in completed.stdout

    def test_an_unrelated_base_move_still_proceeds(self, sandbox: Sandbox) -> None:
        sandbox.commit_on("main", {"README.md": "moved on main\n"})
        completed, outputs = sandbox.run_step(_FRESH)
        assert (completed.returncode, outputs) == (0, {"proceed": "true"})

    def test_a_component_set_change_is_left_red(self, sandbox: Sandbox) -> None:
        sandbox.regenerated(_shrunk_sbom())
        sandbox.run_step(_FRESH)
        completed, outputs = sandbox.run_step(_DRIFT)
        assert (completed.returncode, outputs) == (0, {"verdict": "component-set-changed"})
        assert sandbox.remote_tip() == sandbox.head_sha

    def test_an_already_synced_head_is_unchanged(self, sandbox: Sandbox) -> None:
        sandbox.regenerated(_SBOM.read_bytes())
        sandbox.run_step(_FRESH)
        completed, outputs = sandbox.run_step(_DRIFT)
        assert (completed.returncode, outputs) == (0, {"verdict": "unchanged"})


class TestFailsLoud:
    def test_a_head_racing_the_push_is_rejected_not_overwritten(self, sandbox: Sandbox) -> None:
        sandbox.regenerated(_bumped_sbom())
        for name in (_FRESH, _DRIFT, _PROBE):
            assert sandbox.run_step(name, GH_TOKEN=_TOKEN)[0].returncode == 0
        racing = sandbox.commit_on(_BRANCH, {"uv.lock": "lock v3\n"})
        completed, _ = sandbox.run_step(_PUSH, TEATREE_GH_TOKEN=_TOKEN)
        assert completed.returncode != 0
        assert sandbox.remote_tip() == racing

    def test_a_branch_that_moved_after_the_push_reds_the_re_read(self, sandbox: Sandbox) -> None:
        sandbox.regenerated(_bumped_sbom())
        outputs = sandbox.sync()
        sandbox.commit_on(_BRANCH, {"uv.lock": "lock v3\n"})
        completed, _ = sandbox.run_step(_VERIFY, PUSHED=outputs["commit"])
        assert completed.returncode != 0
        assert "::error::" in completed.stderr

    def test_a_missing_artifact_reds_the_classifier(self, sandbox: Sandbox) -> None:
        sandbox.run_step(_FRESH)
        completed, outputs = sandbox.run_step(_DRIFT)
        assert completed.returncode != 0
        assert outputs == {}

    def test_an_unset_credential_names_the_secret(self, sandbox: Sandbox) -> None:
        completed, _ = sandbox.run_step(_PROBE, GH_TOKEN="")
        assert completed.returncode != 0
        assert "TEATREE_GH_TOKEN is unset or rejected" in completed.stderr

    def test_a_rejected_credential_names_the_secret(self, sandbox: Sandbox) -> None:
        completed, _ = sandbox.run_step(_PROBE, GH_TOKEN=_TOKEN, GH_STUB_EXIT="1")
        assert completed.returncode != 0
        assert "TEATREE_GH_TOKEN is unset or rejected" in completed.stderr

    def test_an_accepted_credential_passes(self, sandbox: Sandbox) -> None:
        completed, _ = sandbox.run_step(_PROBE, GH_TOKEN=_TOKEN)
        assert completed.returncode == 0, completed.stderr
