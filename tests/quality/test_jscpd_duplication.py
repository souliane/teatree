"""Conformance ledger for the jscpd duplication gate (layer 4 anti-drift).

Four invariants.

Config pin: ``.jscpd.json`` keeps the design thresholds (min-lines 6,
min-tokens 40, threshold 1.5 = the blocking duplication ratchet that locks in
the dedup-burst cleanup — only shrinks) and a high max-lines/max-size so jscpd
never silently skips a large source file. A loosening turns this red.

Version pin: the prek hook, this scan and the test image run ONE pinned jscpd.
A floating ``jscpd@4`` re-resolved against the registry to 4.3.0 while the hook
stayed at 4.2.4, so two consumers enforced one config on different engines.

Gate relocation: the duplication THRESHOLD is enforced by the prek ``jscpd``
hook that CI's ``lint`` job runs whole-tree, not by a test. Nothing that gated
before is ungated after — these assertions pin that hook and the dedicated
``jscpd-scan`` job in place of the test they replaced.

Scan coverage: every ``src/teatree/**/*.py`` file large enough to contain a
``minLines``-line clone (>= ``minLines`` physical lines, migrations excluded) is
in jscpd's analyzed set. A file below ``minLines`` cannot hold a clone of that
size, so jscpd legitimately omits it. This is the openclaw scan-coverage
pattern: no clone-capable source escapes the scanner.

jscpd's default ``max-lines`` (1000) and ``max-size`` (100kb) silently drop a
large file — the bug this assertion pins.
"""

import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _REPO_ROOT / ".jscpd.json"
_SRC = _REPO_ROOT / "src" / "teatree"
_PRECOMMIT = _REPO_ROOT / ".pre-commit-config.yaml"
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_TEST_DOCKERFILE = _REPO_ROOT / "dev" / "Dockerfile.test"
_NPX = shutil.which("npx") or "npx"

#: The one jscpd every consumer runs. A range re-resolves per invocation (#4672).
_JSCPD_PIN = "4.2.4"

#: Opt-in for the whole-tree scan, set by CI's dedicated `jscpd-scan` job.
_SCAN_ENV = "TEATREE_JSCPD_SCAN"

#: The scan's own budget, held strictly under the pytest ceiling below so jscpd's
#: timeout fires first and reports itself instead of a bare selector traceback.
_SCAN_BUDGET_SECONDS = 900
_SCAN_CEILING_SECONDS = 1200


def _line_count(path: Path) -> int:
    return path.read_text(encoding="utf-8").count("\n") + 1


@pytest.fixture(scope="module")
def config() -> dict:
    return json.loads(_CONFIG.read_text(encoding="utf-8"))


class TestConfigPin:
    def test_thresholds_match_design(self, config: dict) -> None:
        assert config["minLines"] == 6
        assert config["minTokens"] == 40
        assert math.isclose(config["threshold"], 1.5)

    def test_python_format_only(self, config: dict) -> None:
        assert config["format"] == ["python"]

    def test_max_lines_and_size_prevent_silent_skip(self, config: dict) -> None:
        biggest = max(_line_count(p) for p in _SRC.rglob("*.py"))
        assert int(config["maxLines"]) > biggest
        assert config["maxSize"].endswith(("mb", "MB"))


def _local_hooks() -> list[dict]:
    config = yaml.safe_load(_PRECOMMIT.read_text(encoding="utf-8"))
    return [hook for repo in config.get("repos", []) for hook in repo.get("hooks", [])]


def _jscpd_hook() -> dict:
    hooks = [hook for hook in _local_hooks() if hook.get("id") == "jscpd"]
    assert hooks, "the prek `jscpd` hook is the whole-tree duplication gate -- it must exist"
    return hooks[0]


def _ci_jobs() -> dict:
    return yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _job_text(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in job.get("steps", []))


class TestDuplicationThresholdStaysGated:
    """The threshold test was deleted as redundant; these pin what replaced it (#4672).

    ``jscpd --config .jscpd.json src/teatree`` asserted exit 0 inside a 300s-capped
    pytest shard. The prek hook runs the identical command whole-tree in CI's ``lint``
    job, with jscpd baked into the lint image and no per-test ceiling.
    """

    def test_prek_hook_runs_the_whole_tree_scan(self) -> None:
        entry = _jscpd_hook()["entry"]
        assert ".jscpd.json" in entry, f"the jscpd hook must read the shared config: {entry}"
        assert "src/teatree" in entry, f"the jscpd hook must scan the whole source tree: {entry}"

    def test_lint_job_does_not_skip_the_hook(self) -> None:
        for step in _ci_jobs()["lint"].get("steps", []):
            skipped = str(step.get("env", {}).get("SKIP", "")).split(",")
            assert "jscpd" not in skipped, (
                "CI's `lint` job is the only remaining enforcement of the duplication "
                "threshold -- SKIP-ing the jscpd hook there ungates it entirely."
            )


class TestVersionPin:
    """One jscpd across the hook, the image and this scan (#4672)."""

    def test_prek_hook_pins_the_shared_version(self) -> None:
        deps = _jscpd_hook()["additional_dependencies"]
        assert f"jscpd@{_JSCPD_PIN}" in deps, f"prek hook must pin jscpd@{_JSCPD_PIN}, got {deps}"

    def test_test_image_bakes_the_shared_version(self) -> None:
        dockerfile = _TEST_DOCKERFILE.read_text(encoding="utf-8")
        assert f"jscpd@{_JSCPD_PIN}" in dockerfile, (
            f"dev/Dockerfile.test must bake jscpd@{_JSCPD_PIN} so the scan job runs the "
            "pinned engine instead of resolving one from the registry mid-timeout."
        )


class TestScanJobIsWiredIn:
    """The relocated scan runs once, unsharded, and still reds the required context (#4672)."""

    def test_scan_job_enables_the_opt_in_and_runs_the_ledger(self) -> None:
        body = _job_text(_ci_jobs()["jscpd-scan"])
        assert f"{_SCAN_ENV}=1" in body, f"the jscpd-scan job must set {_SCAN_ENV}=1 to un-skip the scan"
        assert "tests/quality/test_jscpd_duplication.py" in body, "the jscpd-scan job must run this ledger"

    def test_scan_job_gates_the_required_test_context(self) -> None:
        combiner = _ci_jobs()["test"]
        assert "jscpd-scan" in combiner["needs"], (
            "`test (3.13)` is the required context -- the relocated scan must be a `needs:` of "
            "the combiner, or a scan failure would no longer block merge."
        )
        assert "jscpd-scan" in _job_text(combiner), (
            "the combiner must REQUIRE the jscpd-scan result, not merely depend on it: a "
            "`needs:` alone still passes when the needed job failed under `always()`."
        )

    def test_shard_lane_does_not_deselect_the_scan(self) -> None:
        shard = _job_text(_ci_jobs()["test-shard"])
        assert "not whole_tree_scan" not in shard, "the scan is relocated by an env-gated skipif, not a marker filter"
        assert " -m " not in shard, (
            "the shard lane must carry NO `-m` filter: `shard_stats_plugin` records `selected` "
            "AFTER collection_modifyitems, so a marker deselection makes sum(selected) < total "
            "and fails check_shard_completeness.py on the required context."
        )


def _expected_clone_capable_files(min_lines: int) -> set[Path]:
    return {p.resolve() for p in _SRC.rglob("*.py") if "migrations" not in p.parts and _line_count(p) >= min_lines}


def _jscpd_argv(*args: str) -> list[str]:
    binary = shutil.which("jscpd")
    return [binary, *args] if binary else [_NPX, "--yes", f"jscpd@{_JSCPD_PIN}", *args]


def _scan_scale() -> str:
    sources = [p for p in _SRC.rglob("*.py") if "migrations" not in p.parts]
    return f"{len(sources)} files / {sum(_line_count(p) for p in sources)} lines"


def _run_jscpd(*args: str) -> subprocess.CompletedProcess[str]:
    argv = _jscpd_argv(*args)
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv, cwd=_REPO_ROOT, check=False, capture_output=True, text=True, timeout=_SCAN_BUDGET_SECONDS
        )
    except subprocess.TimeoutExpired as expired:
        pytest.fail(
            f"jscpd did not return in {_SCAN_BUDGET_SECONDS}s (scanning {_scan_scale()}).\n"
            f"command: {shlex.join(argv)}\n"
            f"stdout: {expired.stdout or ''}\nstderr: {expired.stderr or ''}"
        )
    # Direction 2 of #4672: elapsed + scale on the PASS path is what makes a future
    # slowdown legible without downloading a job log; the job runs pytest with `-s`.
    sys.stderr.write(f"jscpd: {time.monotonic() - started:.1f}s for {_scan_scale()}\n")
    return result


@pytest.mark.push_heavy
@pytest.mark.timeout(_SCAN_CEILING_SECONDS)
@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("jscpd") is None and shutil.which("npx") is None, reason="neither jscpd nor npx (node) on PATH"
)
@pytest.mark.skipif(
    os.environ.get(_SCAN_ENV) != "1",
    reason=f"whole-tree jscpd scan (~310s) runs in CI's dedicated jscpd-scan job; set {_SCAN_ENV}=1 to run it here",
)
class TestScanCoverage:
    # pytest 9.1 deprecates a class-scoped fixture defined as an INSTANCE method;
    # this one returns a value rather than setting instance state, so @classmethod
    # is a faithful conversion.
    @pytest.fixture(scope="class")
    @classmethod
    def analyzed(cls, tmp_path_factory: pytest.TempPathFactory) -> set[Path]:
        out = tmp_path_factory.mktemp("jscpd")
        # Report `sources` come back relative to `cwd`, so resolve them against it.
        _run_jscpd(
            "--config",
            str(_CONFIG),
            "--reporters",
            "json",
            "--output",
            str(out),
            "--silent",
            str(_SRC.relative_to(_REPO_ROOT)),
        )
        report = json.loads((out / "jscpd-report.json").read_text(encoding="utf-8"))
        return {(_REPO_ROOT / p).resolve() for p in report["statistics"]["formats"]["python"]["sources"]}

    def test_no_clone_capable_file_escapes(self, analyzed: set[Path], config: dict) -> None:
        expected = _expected_clone_capable_files(int(config["minLines"]))
        escaped = sorted(p.relative_to(_REPO_ROOT).as_posix() for p in expected - analyzed)
        assert not escaped, f"source files not scanned by jscpd: {escaped}"
