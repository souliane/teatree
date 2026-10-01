"""Tests for the SBOM drift classifier the Dependabot SBOM sync acts on (#4907).

A Dependabot uv bump moves ``uv.lock`` and never ``dist/sbom.json``, so the
required ``sbom`` gate reds on every one. The follow-up workflow copies CI's own
regenerated SBOM onto the branch, but only when this classifier says the two
files differ in component versions alone. Its refusals are the tests that matter:
an added, removed, renamed or re-referenced component must never read as a
version move, or the sync would land a component-set change no human looked at.

Every fixture starts from the real committed ``dist/sbom.json`` so the shapes the
classifier sees are the generator's, not a hand-written approximation.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.ci.sbom_drift import (
    UnreadableBomError,
    Verdict,
    classify,
    component_set_delta,
    main,
    parse_bom,
    version_moves,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SBOM = _REPO_ROOT / "dist" / "sbom.json"
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "sbom_drift.py"


def _committed() -> bytes:
    return _SBOM.read_bytes()


def _bom() -> dict[str, Any]:
    return json.loads(_committed())


def _dump(bom: dict[str, Any]) -> bytes:
    return (json.dumps(bom, indent=2) + "\n").encode()


def _component(bom: dict[str, Any], *, with_marker: bool = False) -> dict[str, Any]:
    return next(c for c in bom["components"] if (" ; " in c["description"]) is with_marker)


def _bump(component: dict[str, Any], new: str) -> None:
    old = component["version"]
    component["version"] = new
    component["purl"] = component["purl"].replace(f"@{old}", f"@{new}")
    component["description"] = component["description"].replace(f"=={old}", f"=={new}")


def _text_bump(raw: bytes, name: str, new: str) -> bytes:
    """Edit only the three lines a lockfile-only bump moves; every other byte stays as generated."""
    old = next(c for c in json.loads(raw)["components"] if c["name"] == name)["version"]
    text = raw.decode()
    start = text.index(f": {name}=={old}")
    version_line = f'"version": "{old}"'
    end = text.index(version_line, text.index(f'"name": "{name}"', start)) + len(version_line)
    block = (
        text[start:end]
        .replace(f"=={old}", f"=={new}", 1)
        .replace(f'@{old}"', f'@{new}"', 1)
        .replace(version_line, f'"version": "{new}"', 1)
    )
    return (text[:start] + block + text[end:]).encode()


def _changed_lines(before: bytes, after: bytes) -> int:
    pairs = zip(before.decode().splitlines(), after.decode().splitlines(), strict=True)
    return sum(1 for old, new in pairs if old != new)


class TestFixtureMirrorsTheLiveShape:
    def test_a_text_bump_moves_exactly_the_three_lines_dependabot_leaves_stale(self) -> None:
        name = _component(_bom())["name"]
        assert _changed_lines(_committed(), _text_bump(_committed(), name, "999.0.0")) == 3


class TestVersionOnly:
    def test_identical_bytes_are_unchanged(self) -> None:
        assert classify(_committed(), _committed()) is Verdict.UNCHANGED

    def test_a_lockfile_only_bump_is_version_only(self) -> None:
        name = _component(_bom())["name"]
        assert classify(_committed(), _text_bump(_committed(), name, "999.0.0")) is Verdict.VERSION_ONLY

    def test_a_bump_of_a_component_carrying_an_environment_marker_is_version_only(self) -> None:
        bom = _bom()
        _bump(_component(bom, with_marker=True), "999.0.0")
        assert classify(_committed(), _dump(bom)) is Verdict.VERSION_ONLY

    def test_a_generator_bump_is_version_only(self) -> None:
        bom = _bom()
        bom["metadata"]["tools"]["components"][0]["version"] = "999.0.0"
        assert classify(_committed(), _dump(bom)) is Verdict.VERSION_ONLY

    def test_the_move_is_reported_by_name(self) -> None:
        bom = _bom()
        component = _component(bom)
        before = component["version"]
        _bump(component, "999.0.0")
        moves = version_moves(parse_bom(_committed()), bom)
        assert [(m.name, m.before, m.after) for m in moves] == [(component["name"], before, "999.0.0")]


class TestComponentSetChanged:
    def test_an_added_component_is_refused(self) -> None:
        bom = _bom()
        added = {**_component(bom), "bom-ref": "requirements-L9999", "name": "brand-new"}
        bom["components"].append(added)
        bom["dependencies"].append({"ref": "requirements-L9999"})
        assert classify(_committed(), _dump(bom)) is Verdict.COMPONENT_SET_CHANGED

    def test_a_removed_component_is_refused(self) -> None:
        bom = _bom()
        removed = bom["components"].pop()
        bom["dependencies"] = [d for d in bom["dependencies"] if d["ref"] != removed["bom-ref"]]
        assert classify(_committed(), _dump(bom)) is Verdict.COMPONENT_SET_CHANGED

    def test_a_renamed_component_at_a_kept_bom_ref_is_refused(self) -> None:
        bom = _bom()
        component = _component(bom)
        old = component["name"]
        component["name"] = "impostor"
        component["purl"] = component["purl"].replace(f"/{old}@", "/impostor@")
        component["description"] = component["description"].replace(f": {old}==", ": impostor==")
        assert classify(_committed(), _dump(bom)) is Verdict.COMPONENT_SET_CHANGED

    def test_a_renumbered_bom_ref_is_refused(self) -> None:
        bom = _bom()
        component = _component(bom)
        old_ref = component["bom-ref"]
        component["bom-ref"] = "requirements-L9999"
        for dependency in bom["dependencies"]:
            if dependency["ref"] == old_ref:
                dependency["ref"] = "requirements-L9999"
        assert classify(_committed(), _dump(bom)) is Verdict.COMPONENT_SET_CHANGED

    def test_a_purl_moving_without_the_component_version_is_refused(self) -> None:
        bom = _bom()
        component = _component(bom)
        component["purl"] = component["purl"].replace(f"@{component['version']}", "@999.0.0")
        assert classify(_committed(), _dump(bom)) is Verdict.COMPONENT_SET_CHANGED

    def test_a_changed_environment_marker_is_refused(self) -> None:
        bom = _bom()
        component = _component(bom, with_marker=True)
        component["description"] = component["description"].split(" ; ")[0] + " ; sys_platform == 'linux'"
        assert classify(_committed(), _dump(bom)) is Verdict.COMPONENT_SET_CHANGED

    def test_a_new_key_on_a_component_is_refused(self) -> None:
        bom = _bom()
        _component(bom)["hashes"] = [{"alg": "SHA-256", "content": "0" * 64}]
        assert classify(_committed(), _dump(bom)) is Verdict.COMPONENT_SET_CHANGED

    def test_a_project_version_change_is_refused(self) -> None:
        bom = _bom()
        bom["metadata"]["component"]["version"] = "999.0.0"
        assert classify(_committed(), _dump(bom)) is Verdict.COMPONENT_SET_CHANGED

    def test_a_dependency_graph_change_is_refused(self) -> None:
        bom = _bom()
        bom["dependencies"].pop()
        assert classify(_committed(), _dump(bom)) is Verdict.COMPONENT_SET_CHANGED

    def test_the_delta_names_what_was_added_and_removed(self) -> None:
        bom = _bom()
        removed = bom["components"].pop()
        bom["components"].append({**_component(bom), "name": "brand-new"})
        assert component_set_delta(parse_bom(_committed()), bom) == (("brand-new",), (removed["name"],))


class TestUnreadableInput:
    @pytest.mark.parametrize(
        "raw",
        [
            b"not json",
            b"[]",
            b'{"bomFormat": "SPDX", "components": []}',
            b'{"bomFormat": "CycloneDX", "components": "nope"}',
            b'{"bomFormat": "CycloneDX", "components": ["nope"]}',
        ],
        ids=["not-json", "not-an-object", "not-cyclonedx", "components-not-a-list", "component-not-an-object"],
    )
    def test_is_refused_rather_than_classified(self, raw: bytes) -> None:
        with pytest.raises(UnreadableBomError):
            classify(_committed(), raw)


def _run_main(tmp_path: Path, regenerated: bytes) -> tuple[int, str]:
    committed = tmp_path / "committed.json"
    committed.write_bytes(_committed())
    fresh = tmp_path / "regenerated.json"
    fresh.write_bytes(regenerated)
    output = tmp_path / "gh-output"
    code = main(["--committed", str(committed), "--regenerated", str(fresh), "--github-output", str(output)])
    return code, output.read_text(encoding="utf-8") if output.exists() else ""


class TestMain:
    def test_writes_a_version_only_verdict_and_names_the_move(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        name = _component(_bom())["name"]
        code, output = _run_main(tmp_path, _text_bump(_committed(), name, "999.0.0"))
        assert (code, output) == (0, "verdict=version-only\n")
        assert f"{name} " in capsys.readouterr().out

    def test_writes_an_unchanged_verdict(self, tmp_path: Path) -> None:
        assert _run_main(tmp_path, _committed()) == (0, "verdict=unchanged\n")

    def test_a_component_set_change_warns_and_names_the_manual_regeneration(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bom = _bom()
        bom["components"].pop()
        code, output = _run_main(tmp_path, _dump(bom))
        assert (code, output) == (0, "verdict=component-set-changed\n")
        out = capsys.readouterr().out
        assert "::warning::" in out
        assert "scripts/hooks/generate_sbom.sh" in out

    def test_a_missing_file_fails_loud_and_writes_no_verdict(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        output = tmp_path / "gh-output"
        argv = ["--committed", str(tmp_path / "absent.json"), "--regenerated", str(_SBOM)]
        code = main([*argv, "--github-output", str(output)])
        assert code != 0
        assert not output.exists()
        assert "::error::" in capsys.readouterr().err

    def test_a_non_cyclonedx_file_fails_loud(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code, output = _run_main(tmp_path, b'{"bomFormat": "SPDX"}')
        assert (code != 0, output) == (True, "")
        assert "::error::" in capsys.readouterr().err

    def test_runs_on_stdlib_alone(self, tmp_path: Path) -> None:
        output = tmp_path / "gh-output"
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                str(_SCRIPT),
                "--committed",
                str(_SBOM),
                "--regenerated",
                str(_SBOM),
                "--github-output",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=tmp_path,
        )
        assert completed.returncode == 0, completed.stderr
        assert output.read_text(encoding="utf-8") == "verdict=unchanged\n"
