# test-path: cross-cutting — drives deploy/entrypoint.sh and deploy/Dockerfile (no src mirror).
"""The deploy entrypoint survives an upgrade: an old image, a first boot, a flaky skill source.

Each case runs the entrypoint's REAL shell functions in a bash subprocess against a
failure the reviewed head hit. A fast-forwarded checkout's entrypoint on an image that
predates it runs the image's own baked entrypoint instead of crash-looping the worker.
``init`` finds its GitHub token through the owning overlay's ``github_token_pass_key``
in the control DB, before or after 0104 moved the key out of the overlay registry. A
skill source that fails once no longer takes init, and the stack, down. Re-seeding the
container GPG home can never clear or chmod the host's home.
"""

import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
ENTRYPOINT = DEPLOY / "entrypoint.sh"
DOCKERFILE = DEPLOY / "Dockerfile"
_BASH = shutil.which("bash") or "bash"


def _function(name: str) -> str:
    lines = ENTRYPOINT.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"{name}() {{")
    return "\n".join(lines[start : lines.index("}", start) + 1])


def _bash(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], **(env or {})},
        check=False,
    )


def _executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class TestAnOldImageRunsItsOwnEntrypoint:
    def _boot(self, tmp_path: Path, contract: str | None) -> str:
        baked = _executable(tmp_path / "baked.sh", "#!/usr/bin/env bash\necho baked-entrypoint\n")
        checkout = tmp_path / "checkout"
        _executable(checkout / "deploy" / "watchdog.sh", "echo checkout-entrypoint\n")
        marker = tmp_path / "entrypoint-contract"
        if contract is not None:
            marker.write_text(contract, encoding="utf-8")
        result = subprocess.run(
            [_BASH, str(ENTRYPOINT)],
            capture_output=True,
            text=True,
            env={
                "PATH": os.environ["PATH"],
                "TEATREE_ROLE": "watchdog",
                "TEATREE_BAKED_ENTRYPOINT": str(baked),
                "TEATREE_ENTRYPOINT_CONTRACT_FILE": str(marker),
                "TEATREE_DEPLOY_CHECKOUT": str(checkout),
            },
            check=False,
        )
        return result.stdout.strip()

    def test_an_image_without_the_contract_marker_runs_its_baked_entrypoint(self, tmp_path: Path) -> None:
        assert self._boot(tmp_path, None) == "baked-entrypoint"

    def test_an_unreadable_marker_counts_as_the_oldest_image(self, tmp_path: Path) -> None:
        assert self._boot(tmp_path, "garbage") == "baked-entrypoint"

    def test_an_image_meeting_the_contract_runs_the_checkout_entrypoint(self, tmp_path: Path) -> None:
        assert self._boot(tmp_path, "1\n") == "checkout-entrypoint"

    def test_the_dockerfile_bakes_the_contract_the_entrypoint_requires(self) -> None:
        required = re.search(r"^ENTRYPOINT_IMAGE_CONTRACT=(\d+)$", ENTRYPOINT.read_text(encoding="utf-8"), re.MULTILINE)
        baked = re.search(r"echo (\d+) > /usr/local/share/teatree/entrypoint-contract", DOCKERFILE.read_text())
        assert required is not None
        assert baked is not None
        assert baked.group(1) == required.group(1)


def test_the_image_installs_the_github_credential_helper_system_wide() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert '"credential.https://github.com.helper" "!gh auth git-credential"' in dockerfile


def _control_db(directory: Path, rows: list[tuple[str, str, object]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    db = directory / "db.sqlite3"
    connection = sqlite3.connect(db)
    try:
        connection.execute("CREATE TABLE teatree_config_setting (scope TEXT, key TEXT, value TEXT)")
        connection.executemany(
            "INSERT INTO teatree_config_setting VALUES (?, ?, ?)",
            [(scope, key, json.dumps(value)) for scope, key, value in rows],
        )
        connection.commit()
    finally:
        connection.close()
    return db


class TestInitFindsItsTokenThroughTheOverlayRoute:
    def _route(self, control_dir: Path) -> str:
        script = "\n".join(
            [_function("gh_repo_slug"), _function("db_routed_github_pass_key"), "db_routed_github_pass_key"]
        )
        result = _bash(
            script,
            {"T3_CONTROL_DB_DIR": str(control_dir), "REPO_URL": "https://github.com/souliane/teatree.git"},
        )
        return result.stdout.strip()

    def test_a_db_not_yet_migrated_reads_the_key_from_the_overlay_registry(self, tmp_path: Path) -> None:
        registry = {"t3-teatree": {"github_token_pass_key": "github/owner/pat", "workspace_repos": []}}
        _control_db(tmp_path, [("", "overlays", registry)])

        assert self._route(tmp_path) == "github/owner/pat"

    def test_a_migrated_db_reads_the_overlays_own_row(self, tmp_path: Path) -> None:
        _control_db(
            tmp_path,
            [
                ("", "overlays", {"t3-teatree": {"workspace_repos": []}}),
                ("t3-teatree", "github_token_pass_key", "gh/row"),
            ],
        )

        assert self._route(tmp_path) == "gh/row"

    def test_the_overlay_owning_the_deploy_repo_wins(self, tmp_path: Path) -> None:
        registry = {
            "aaa-other": {"github_token_pass_key": "gh/other", "workspace_repos": ["acme/widget"]},
            "zzz-core": {"github_token_pass_key": "gh/core", "workspace_repos": ["souliane/teatree"]},
        }
        _control_db(tmp_path, [("", "overlays", registry)])

        assert self._route(tmp_path) == "gh/core"

    def test_a_fresh_box_with_no_control_db_has_no_route(self, tmp_path: Path) -> None:
        assert self._route(tmp_path / "absent") == ""

    def test_the_preflight_sources_the_routed_entry_when_no_token_is_exported(self) -> None:
        preflight = _function("init_preflight")
        assert "db_routed_github_pass_key" in preflight
        assert preflight.index("db_routed_github_pass_key") < preflight.index("MISSING TEATREE_GH_TOKEN")


class TestAFlakySkillSourceDoesNotTakeTheStackDown:
    def _prepare(self, tmp_path: Path, failures: int) -> subprocess.CompletedProcess[str]:
        calls = tmp_path / "calls"
        script = "\n".join(
            [
                "set -euo pipefail",
                "seed_claude_settings() { :; }",
                "sleep() { :; }",
                f't3() {{ echo x >> {calls}; [ "$(wc -l < {calls})" -gt {failures} ]; }}',
                _function("prepare_agent_homes"),
                "prepare_agent_homes",
                f"wc -l < {calls}",
            ]
        )
        return _bash(script)

    def test_a_transient_failure_is_retried(self, tmp_path: Path) -> None:
        result = self._prepare(tmp_path, failures=1)
        assert result.returncode == 0
        assert result.stdout.strip() == "2"

    def test_a_persistent_failure_warns_and_lets_init_finish(self, tmp_path: Path) -> None:
        result = self._prepare(tmp_path, failures=99)
        assert result.returncode == 0
        assert result.stdout.strip() == "3"
        assert "verify_agent_skills" in result.stderr


_CLEAR = (_function("same_directory"), _function("clear_container_gnupg_home"))


class TestTheHostGpgHomeIsNeverClearedOrChmodded:
    def test_a_container_home_that_is_the_host_home_is_refused(self, tmp_path: Path) -> None:
        host = tmp_path / "gnupg"
        host.mkdir()
        (host / "private-keys-v1.d").mkdir()
        script = "\n".join(
            [
                _function("same_directory"),
                _function("clear_container_gnupg_home"),
                f"clear_container_gnupg_home {host} {host}",
            ]
        )

        result = _bash(script)

        assert result.returncode == 1
        assert (host / "private-keys-v1.d").is_dir()

    def test_a_link_to_the_host_home_is_unlinked_not_followed(self, tmp_path: Path) -> None:
        host = tmp_path / "gnupg"
        (host / "private-keys-v1.d").mkdir(parents=True)
        link = tmp_path / "run" / "gnupg"
        link.parent.mkdir()
        link.symlink_to(host)
        script = "\n".join(
            [
                _function("same_directory"),
                _function("clear_container_gnupg_home"),
                f"clear_container_gnupg_home {link} {host}",
            ]
        )

        assert _bash(script).returncode == 0
        assert not link.is_symlink()
        assert (host / "private-keys-v1.d").is_dir()

    def test_an_adopted_home_already_private_is_left_untouched(self, tmp_path: Path) -> None:
        host = tmp_path / "gnupg"
        host.mkdir(mode=0o700)
        link = tmp_path / "adopted"
        link.symlink_to(host)
        before = host.stat().st_ctime_ns

        _bash(_function("normalise_gnupg_home_mode") + "\nnormalise_gnupg_home_mode", {"GNUPGHOME": str(link)})

        assert host.stat().st_ctime_ns == before
