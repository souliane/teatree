"""Run the named regression-detector semgrep rules and parse their findings.

semgrep is not a teatree runtime dependency (its dep tree conflicts with the
project's): it is SHA-pinned in ``.pre-commit-config.yaml`` and invoked through
its isolated pre-commit env. Here it is invoked through ``uvx semgrep@<floor>``
so the conformance test (and any caller) runs the SAME pinned engine without
adding semgrep to the project lock.

``SEMGREP_VERSION_FLOOR`` is the minimum that parses the codebase's own PEP 695
``type`` statements (1.139.0 silently drops a whole file's analysis on the first
``type`` alias, masking real findings). A future bump fails an explicit test
rather than a silent CI no-op.
"""

import json
import shutil
from pathlib import Path

from teatree.quality.regression_catalog import repo_root
from teatree.utils.run import run_allowed_to_fail

SEMGREP_VERSION_FLOOR = "1.165.0"


class SemgrepUnavailableError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"semgrep engine unavailable: {reason}")


def _semgrep_argv() -> list[str]:
    if shutil.which("semgrep") is not None:
        return ["semgrep"]
    if shutil.which("uvx") is not None:
        return ["uvx", f"semgrep@{SEMGREP_VERSION_FLOOR}"]
    reason = "neither 'semgrep' nor 'uvx' is on PATH"
    raise SemgrepUnavailableError(reason)


def semgrep_invocable() -> bool:
    try:
        argv = _semgrep_argv()
    except SemgrepUnavailableError:
        return False
    result = run_allowed_to_fail([*argv, "--version"], expected_codes=None, timeout=180)
    return result.returncode == 0


def scan_findings(config_dir: Path, root: Path | None = None) -> list[dict]:
    base = root or repo_root()
    result = run_allowed_to_fail(
        [*_semgrep_argv(), "scan", "--config", str(config_dir), "--json", "--quiet"],
        expected_codes=None,
        cwd=base,
        timeout=600,
    )
    if not result.stdout.strip():
        reason = f"no JSON output (stderr: {result.stderr.strip()})"
        raise SemgrepUnavailableError(reason)
    return json.loads(result.stdout)["results"]
