"""The skills CLI's install record, read where the CLI itself writes it."""

import json
from pathlib import Path

import pytest

from teatree.provisioning.skills_lock import lock_path, read_install_refs

_SHA = "d0008a3c1e5f4b2a9d8e7f6a5b4c3d2e1f0a9b8c"


def _write(path: Path, body: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")


def _record(version: int = 3, **skills: dict[str, str]) -> dict[str, object]:
    return {"version": version, "skills": skills}


def test_the_record_lives_under_the_agents_dir_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    assert lock_path(tmp_path) == tmp_path / ".agents" / ".skill-lock.json"


def test_xdg_state_home_moves_the_record_exactly_as_the_cli_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    _write(tmp_path / ".agents" / ".skill-lock.json", _record(stale={"source": "x/y", "ref": "stale"}))
    _write(state / "skills" / ".skill-lock.json", _record(fresh={"source": "x/y", "ref": _SHA}))

    assert read_install_refs(tmp_path) == {"fresh": ("x/y", _SHA)}


def test_source_and_ref_are_lowercased_and_a_missing_ref_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    _write(
        tmp_path / ".agents" / ".skill-lock.json",
        _record(a={"source": "Souliane/Skills", "ref": _SHA.upper()}, b={"source": "souliane/skills"}),
    )

    assert read_install_refs(tmp_path) == {"a": ("souliane/skills", _SHA), "b": ("souliane/skills", "")}


@pytest.mark.parametrize(
    "body",
    [None, "{not json", "[]", json.dumps(_record(version=4)), json.dumps({"version": 3}), json.dumps(_record(a="x"))],
    ids=["absent", "garbage", "not-a-mapping", "other-schema", "no-skills", "entry-not-a-mapping"],
)
def test_an_absent_unparsable_or_other_schema_record_is_unknown_never_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str | None
) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    if body is not None:
        _write(tmp_path / ".agents" / ".skill-lock.json", body)

    assert read_install_refs(tmp_path) is None
