"""The committed App Server contract is the pinned codex CLI's own schema, not a hand copy.

``test_codex_app_server`` checks every request the harness sends against
``fixtures/codex_app_server/<pin>-contract.json``. That only proves compatibility if the
fixture is what the pinned CLI actually publishes for the capabilities the harness
negotiates (``experimentalApi``), so this regenerates the schema from that CLI and compares.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_TEATREE = Path(__file__).resolve().parents[2]
_PIN = re.search(
    r"@openai/codex@(\d+\.\d+\.\d+)\b", (_TEATREE / "deploy" / "Dockerfile").read_text(encoding="utf-8")
).group(1)
_CONTRACT = _TEATREE / "tests" / "fixtures" / "codex_app_server" / f"{_PIN}-contract.json"


def _codex_version(binary: str) -> str:
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30, check=False)
    except OSError:
        return "unrunnable"
    return result.stdout.strip() or "unknown"


@pytest.fixture(scope="module")
def pinned_schema(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    binary = shutil.which("codex")
    found = _codex_version(binary) if binary else "no codex on PATH"
    if binary is None or found != f"codex-cli {_PIN}":
        pytest.skip(
            f"UNVERIFIED: the image pins codex {_PIN} but this venue has {found}, so {_CONTRACT.name} "
            "was not compared with the real CLI's schema here."
        )
    out = tmp_path_factory.mktemp("codex-schema")
    subprocess.run(
        [binary, "app-server", "generate-json-schema", "--experimental", "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return {
        name: json.loads((out / f"{name}.json").read_text(encoding="utf-8"))
        for name in ("ClientRequest", "ClientNotification")
    }


def _params_by_method(schema: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    definitions = schema.get("definitions", {})
    methods: dict[str, dict[str, list[str]]] = {}
    for variant in schema["oneOf"]:
        properties = variant.get("properties", {})
        method = properties["method"]["enum"][0]
        ref = properties.get("params", {}).get("$ref", "")
        params = definitions.get(ref.rsplit("/", 1)[-1], {}) if ref else {}
        methods[method] = {
            "required": sorted(params.get("required", [])),
            "allowed": sorted(params.get("properties", {})),
        }
    return methods


def _sandbox_modes(schema: dict[str, Any]) -> list[str]:
    return sorted(schema["definitions"]["SandboxMode"]["enum"])


def test_the_contract_fixture_is_the_one_for_the_pinned_cli() -> None:
    assert json.loads(_CONTRACT.read_text(encoding="utf-8"))["codex_cli_version"] == _PIN


def test_every_method_the_harness_uses_matches_the_pinned_schema(pinned_schema: dict[str, Any]) -> None:
    published = _params_by_method(pinned_schema["ClientRequest"]) | _params_by_method(
        pinned_schema["ClientNotification"]
    )
    committed = json.loads(_CONTRACT.read_text(encoding="utf-8"))["methods"]

    for method, contract in committed.items():
        assert method in published, f"codex {_PIN} publishes no {method!r}"
        assert set(contract["allowed"]) <= set(published[method]["allowed"]), method
        assert set(published[method]["required"]) <= set(contract["required"]), method


def test_the_sandbox_modes_match_the_pinned_schema(pinned_schema: dict[str, Any]) -> None:
    committed = json.loads(_CONTRACT.read_text(encoding="utf-8"))["sandbox_modes"]

    assert sorted(committed) == _sandbox_modes(pinned_schema["ClientRequest"])
