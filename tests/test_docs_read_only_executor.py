# test-path: cross-cutting — pins the docs against the executor contract in teatree.agents.
"""The docs describe Codex planning through the read-only executor, not the outright refusal it replaced."""

from pathlib import Path

from teatree.agents.codex_read_only_executor import EXECUTOR_LOGIN_ENTRY, EXECUTOR_PHASES, EXECUTOR_SERVICE

_ROOT = Path(__file__).resolve().parents[1]


def _section(doc: str, heading: str) -> str:
    text = (_ROOT / doc).read_text(encoding="utf-8")
    start = text.index(heading)
    level = heading.split(" ", 1)[0]
    end = text.find(f"\n{level} ", start + len(heading))
    return text[start : end if end != -1 else len(text)]


def test_no_doc_still_says_a_managed_lane_never_runs_a_write_barring_phase() -> None:
    docs = [_ROOT / "BLUEPRINT.md", _ROOT / "deploy" / "README.md", *sorted((_ROOT / "docs").rglob("*.md"))]

    assert [doc for doc in docs if "never runs a phase that bars write tools" in doc.read_text(encoding="utf-8")] == []


def test_the_codex_doc_describes_the_executor_by_its_code_names() -> None:
    section = _section("docs/codex-app-server.md", "## Read-only executor")

    assert EXECUTOR_SERVICE in section
    assert EXECUTOR_LOGIN_ENTRY in section
    assert all(f"`{phase}`" in section for phase in EXECUTOR_PHASES)


def test_the_blueprint_names_the_live_probe_hook_and_the_executor_login() -> None:
    text = (_ROOT / "BLUEPRINT.md").read_text(encoding="utf-8")

    assert "read_only_executor_refusal" in text
    assert EXECUTOR_LOGIN_ENTRY in text


def test_the_deploy_security_posture_keeps_the_secrets_file_out_of_reach() -> None:
    section = " ".join(_section("deploy/README.md", "### Security posture: the read-only executor").split())

    assert EXECUTOR_SERVICE in section
    assert "never the directory that holds `deploy/teatree.env`" in section
    assert "The only secret it ever holds" not in section
