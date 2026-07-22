# test-path: cross-cutting — drives deploy/entrypoint.sh (no src mirror).
"""Integration tests for the entrypoint's ~/.claude/settings.json provisioning.

`deploy/entrypoint.sh`'s `seed_claude_settings` writes the deploy-managed Claude
Code settings before `t3 setup` runs, deep-merging (`jq '.[0] * .[1]'`, right
wins) over any EXISTING file so unmanaged keys (`statusLine`, added later by
`t3 setup`) survive a redeploy. A pre-existing settings.json that is INVALID JSON
must be REPLACED with the managed config, not left corrupt: the merge cannot parse
it, and a corrupt settings.json downstream bricks `t3 setup` / the `claude` CLI
and silently drops the managed model + permission mode. The guard keeps init
crash-proof against a hand-corrupted or partially-written settings file.

These run the REAL shell function (extracted verbatim) in a bash subprocess with
the REAL committed template and the REAL pure-stdlib resolver, so the merge /
repair / fresh-write contract is exercised end to end with real `jq`.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="needs bash + jq (present in the deploy image and CI)",
)

_REPO = Path(__file__).resolve().parents[1]
ENTRYPOINT = _REPO / "deploy" / "entrypoint.sh"
TEMPLATE = _REPO / "deploy" / "claude-settings.template.json"
_BASH = shutil.which("bash") or "bash"


def _extract_shell_function(name: str) -> str:
    """Return the verbatim source of shell function *name* from the entrypoint."""
    body: list[str] = []
    capturing = False
    for line in ENTRYPOINT.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}() {{"):
            capturing = True
        if capturing:
            body.append(line)
            if line == "}":
                return "\n".join(body)
    not_found = f"function {name!r} not found in {ENTRYPOINT}"
    raise AssertionError(not_found)


def _run_seed(tmp_path: Path, existing: str | None) -> tuple[Path, subprocess.CompletedProcess[str]]:
    """Run `seed_claude_settings` with *existing* content pre-seeded (or no file).

    Returns the resolved settings.json path and the completed process. Uses the
    REAL committed template + resolver so the merge is exercised with real `jq`.
    """
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    target = home / ".claude" / "settings.json"
    if existing is not None:
        target.write_text(existing, encoding="utf-8")
    func = _extract_shell_function("seed_claude_settings")
    harness = tmp_path / "harness.sh"
    harness.write_text(f"set -euo pipefail\n{func}\nseed_claude_settings\n", encoding="utf-8")
    proc = subprocess.run(
        [_BASH, str(harness)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "CLONE_DIR": str(_REPO),
            "TEATREE_CLAUDE_SETTINGS_TEMPLATE": str(TEMPLATE),
        },
    )
    return target, proc


class TestSeedClaudeSettings:
    def test_corrupt_existing_settings_is_replaced_not_bricked(self, tmp_path: Path) -> None:
        # RED before the guard: an invalid pre-existing settings.json made the merge
        # jq fail, leaving the corrupt file in place (managed keys silently dropped,
        # downstream `t3 setup` / `claude` CLI choke on it). It must be REPLACED.
        target, proc = _run_seed(tmp_path, existing="{invalid json, not parseable")
        assert proc.returncode == 0, proc.stderr
        parsed = json.loads(target.read_text(encoding="utf-8"))  # must be valid JSON now
        assert parsed["model"] == "opusplan"

    def test_no_existing_file_writes_managed_config(self, tmp_path: Path) -> None:
        target, proc = _run_seed(tmp_path, existing=None)
        assert proc.returncode == 0, proc.stderr
        parsed = json.loads(target.read_text(encoding="utf-8"))
        assert parsed["model"] == "opusplan"

    def test_valid_existing_file_merge_preserves_unmanaged_keys(self, tmp_path: Path) -> None:
        # Anti-regression: a VALID existing file still deep-merges so an unmanaged
        # key (`statusLine`, added later by `t3 setup`) survives the redeploy while
        # the managed model wins.
        existing = json.dumps({"statusLine": {"type": "command", "command": "mine"}, "model": "stale"})
        target, proc = _run_seed(tmp_path, existing=existing)
        assert proc.returncode == 0, proc.stderr
        parsed = json.loads(target.read_text(encoding="utf-8"))
        assert parsed["model"] == "opusplan"  # managed key wins
        assert parsed["statusLine"] == {"type": "command", "command": "mine"}  # unmanaged survives
