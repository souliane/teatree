"""Run the named regression-detector ast-grep rules and parse their findings.

ast-grep is not a teatree runtime dependency: it is pinned (``ASTGREP_PIN``) and
resolved hermetically through ``uvx --from ast-grep-cli==<pin> ast-grep`` (the
same engine the ac-django convention hooks use), falling back to a system
``ast-grep``/``sg`` on PATH. Here it is invoked so the conformance test (and any
caller) runs the SAME pinned engine without adding ast-grep to the project lock.

A future pin bump fails the explicit ``test_engine_is_invocable`` pin rather than
a silent CI no-op.

The scan is restricted to the rule ids declared under the config directory
(``--filter``) so ast-grep's built-in ``unused-suppression`` lint — which fires
on every ``# ast-grep-ignore`` comment left for an *unrelated* ruleset (the
ac-django hooks) — never leaks into the regression findings.
"""

import json
import shutil
from collections.abc import Sequence
from pathlib import Path

import yaml

from teatree.quality.regression_catalog import repo_root
from teatree.utils.run import run_allowed_to_fail

ASTGREP_PIN = "0.42.3"


class AstGrepUnavailableError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"ast-grep engine unavailable: {reason}")


def _astgrep_argv() -> list[str]:
    if shutil.which("uvx") is not None:
        return ["uvx", "--from", f"ast-grep-cli=={ASTGREP_PIN}", "ast-grep"]
    for binary in ("ast-grep", "sg"):
        if shutil.which(binary) is not None:
            return [binary]
    reason = "neither 'uvx' nor a system 'ast-grep'/'sg' is on PATH"
    raise AstGrepUnavailableError(reason)


def astgrep_invocable() -> bool:
    try:
        argv = _astgrep_argv()
    except AstGrepUnavailableError:
        return False
    result = run_allowed_to_fail([*argv, "--version"], expected_codes=None, timeout=180)
    return result.returncode == 0


def _declared_rule_ids(config_dir: Path) -> tuple[str, ...]:
    ids: list[str] = []
    for rule_file in sorted(config_dir.glob("*.yml")):
        loaded = yaml.safe_load(rule_file.read_text(encoding="utf-8"))
        rule_id = loaded.get("id") if isinstance(loaded, dict) else None
        if isinstance(rule_id, str):
            ids.append(rule_id)
    return tuple(ids)


def scan_findings(config_dir: Path, root: Path | None = None, *, paths: Sequence[Path] | None = None) -> list[dict]:
    """Run the blocking rules over the tree, or only over *paths* when given (#122).

    ``paths=None`` (the default) scans the whole tree under *root* — the CI backstop
    path, byte-identical to before this kwarg existed. A NON-empty *paths* appends
    those files as positional args so ast-grep scans ONLY them (the push gate's
    scoped Engine B). An EMPTY *paths* means "no files in scope" and returns ``[]``
    WITHOUT invoking ast-grep — an empty positional list would make ast-grep scan
    the whole tree, the opposite of the intent, so it is short-circuited.
    """
    if paths is not None and len(paths) == 0:
        return []
    base = root or repo_root()
    rule_ids = _declared_rule_ids(config_dir)
    if not rule_ids:
        reason = f"no ast-grep rules under {config_dir}"
        raise AstGrepUnavailableError(reason)
    sgconfig = _sgconfig_for(config_dir)
    cmd = [
        *_astgrep_argv(),
        "scan",
        "--config",
        str(sgconfig),
        "--filter",
        "|".join(rule_ids),
        "--json",
        *([str(p) for p in paths] if paths is not None else []),
    ]
    result = run_allowed_to_fail(cmd, expected_codes=None, cwd=base, timeout=600)
    stripped = result.stdout.strip()
    if not stripped:
        reason = f"no JSON output (stderr: {result.stderr.strip()})"
        raise AstGrepUnavailableError(reason)
    matches = json.loads(stripped)
    return [_normalize(match) for match in matches]


def _sgconfig_for(config_dir: Path) -> Path:
    sgconfig = config_dir.parent / "sgconfig.yml"
    if sgconfig.is_file():
        return sgconfig
    reason = f"no sgconfig.yml beside {config_dir}"
    raise AstGrepUnavailableError(reason)


def _normalize(match: dict) -> dict:
    return {
        "check_id": match.get("ruleId", ""),
        "path": match.get("file", ""),
        "start": {"line": match["range"]["start"]["line"]},
    }
