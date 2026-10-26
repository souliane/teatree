"""`deploy.sh` and `roll.sh` read host tokens only through `secret-read.sh`, so the sealed selector never runs `pass`.

The real script slices run under bash against the real helper; the recording `pass` stub is the one external.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
SYSTEM_PATH = os.defpath.strip(os.pathsep)

BASH = shutil.which("bash", path=SYSTEM_PATH) or "/bin/bash"


PASS_STUB = """#!/bin/sh
printf '%s\\n' "$*" >>"$PASS_LOG"
printf 'value-for-%s\\nsecond-line\\n' "$2"
"""


def _slice(script: str, first: str, last: str) -> str:
    lines = (DEPLOY / script).read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(first))
    end = next(index for index, line in enumerate(lines) if index >= start and line.startswith(last))
    return "\n".join(lines[start : end + 1])


READS = {
    "deploy.sh": _slice("deploy.sh", 'if [ -z "${GITLAB_TOKEN:-}"', "# Services the staged swap"),
    "roll.sh": _slice("roll.sh", 'for pair in "GITLAB_TOKEN', "done"),
}


def _run_reads(script: str, tmp_path: Path, selector: str) -> tuple[list[str], str, bool]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "pass"
    stub.write_text(PASS_STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "pass.log"
    log.write_text("", encoding="utf-8")
    result = subprocess.run(
        [
            str(BASH),
            "--noprofile",
            "--norc",
            "-c",
            f'SCRIPT_DIR="$DEPLOY"\n{READS[script]}\nprintf "\\n%s|%s\\n" "$GITLAB_TOKEN" "$NOTION_TOKEN"\nenv',
        ],
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": f"{stub_dir}{os.pathsep}{SYSTEM_PATH}",
            "DEPLOY": str(DEPLOY),
            "PASS_LOG": str(log),
            "T3_SECRET_STORE": selector,
            "TEATREE_GITLAB_TOKEN_PASS_PATH": "gitlab/pat",
            "NOTION_TOKEN_PASS_PATH": "notion/token",
        },
    )
    lines = result.stdout.splitlines()
    tokens = next(line for line in lines if line.startswith("value-for-") or line == "|")
    return log.read_text(encoding="utf-8").splitlines(), tokens, any(line.startswith("NOTION_TOKEN=") for line in lines)


@pytest.mark.parametrize("script", sorted(READS))
def test_the_sealed_selector_never_runs_pass_and_leaves_the_tokens_to_the_container(
    script: str, tmp_path: Path
) -> None:
    calls, tokens, _ = _run_reads(script, tmp_path, "sealed")
    assert calls == []
    assert tokens == "|"


@pytest.mark.parametrize("script", sorted(READS))
def test_the_pass_selector_reads_each_named_entry_once_and_exports_its_first_line(script: str, tmp_path: Path) -> None:
    calls, tokens, _ = _run_reads(script, tmp_path, "pass")
    assert calls == ["show gitlab/pat", "show notion/token"]
    assert tokens == "value-for-gitlab/pat|value-for-notion/token"


def test_sealed_deploy_does_not_export_an_empty_notion_token(tmp_path: Path) -> None:
    _, _, exported = _run_reads("deploy.sh", tmp_path, "sealed")
    assert not exported
