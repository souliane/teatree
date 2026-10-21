import os
import shutil
import subprocess
from pathlib import Path

import yaml

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
BASH = shutil.which("bash") or "/bin/bash"
LISTENER = "teatree-webhooks"
CONTAINER_HOME = Path("/home") / "teatree"
HOST_HOME = f"${{TEATREE_HOST_HOME:-{CONTAINER_HOME}}}"
CHECKOUT_ENTRYPOINT = f"${{TEATREE_DEPLOY_CHECKOUT:-{CONTAINER_HOME / 'teatree-deploy'}}}/deploy/entrypoint.sh"


def _entrypoint(tmp_path: Path, role: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (
        ("t3", "exit 0"),
        ("python", 'printf "%s\\n" "$0 $*"; printf "%s" "${T3_ADMIN_PASSWORD-unset}" > "$SECRET_CAPTURE"'),
        ("pass", "printf '%s\\n' canary"),
    ):
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        stub.chmod(0o755)
    env = {
        key: value for key, value in os.environ.items() if key != "T3_ADMIN_PASSWORD" and not key.startswith("TEATREE_")
    } | {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TEATREE_ROLE": role,
        "TEATREE_BAKED_ENTRYPOINT": str(tmp_path / "missing-entrypoint"),
        "TEATREE_HOST_GNUPG_DIR": str(tmp_path / "missing-gnupg"),
        "TEATREE_GNUPG_RUNTIME_DIR": str(tmp_path / "runtime-gnupg"),
        "TEATREE_DISK_TMPDIR": str(tmp_path),
        "SECRET_CAPTURE": str(tmp_path / "secret-capture"),
    }
    return subprocess.run(
        [BASH, str(DEPLOY / "entrypoint.sh")], env=env, capture_output=True, text=True, timeout=20, check=False
    )


def test_entrypoint_knows_webhook_listener_role(tmp_path: Path) -> None:
    run = _entrypoint(tmp_path, "webhook-listener")

    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith(f"{tmp_path}/bin/python -m gunicorn ")
    assert "--bind 127.0.0.1:8001" in run.stdout
    assert run.stdout.rstrip().endswith("teatree.webhook_wsgi:application")
    assert _entrypoint(tmp_path, "unrecognized").returncode == 64


def test_listener_execs_before_admin_password_is_sourced(tmp_path: Path) -> None:
    run = _entrypoint(tmp_path, "webhook-listener")

    assert run.returncode == 0, run.stderr
    assert (tmp_path / "secret-capture").read_text(encoding="utf-8") == "unset"


def test_listener_service_has_no_forge_tokens() -> None:
    compose = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    listener = compose["services"][LISTENER]

    assert listener["environment"]["TEATREE_ROLE"] == "webhook-listener"
    assert not any(key.endswith("_TOKEN") for key in listener["environment"])
    assert "env_file" not in listener
    assert any(key.endswith("_TOKEN") for key in compose["services"]["teatree-admin"]["environment"])


def _mount(volume: str | dict[str, object]) -> str:
    if isinstance(volume, str):
        return volume
    return f"{volume['source']}:{volume['target']}" + (":ro" if volume.get("read_only") else "")


def test_listener_mounts_only_its_ledger_secrets_and_read_only_code() -> None:
    listener = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))["services"][LISTENER]

    assert {_mount(volume) for volume in listener["volumes"]} == {
        "teatree_control_db:/var/lib/teatree/control-db",
        f"{HOST_HOME}/.password-store:{CONTAINER_HOME}/.password-store",
        f"{HOST_HOME}/.gnupg:{CONTAINER_HOME}/.gnupg",
        f"{CHECKOUT_ENTRYPOINT}:{CHECKOUT_ENTRYPOINT}:ro",
        f"${{TEATREE_SOURCE_MOUNT:-teatree_src}}:{CONTAINER_HOME}/teatree:ro",
        "teatree_uv:/opt/teatree/uv:ro",
    }
    assert listener["entrypoint"] == ["bash", CHECKOUT_ENTRYPOINT]
    assert listener["init"] is True
