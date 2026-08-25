# test-path: cross-cutting — drives deploy/watchdog.sh + docker-compose.yml (no src mirror).
"""The watchdog's ledgers outlive the container that writes them (#4458).

They were plain files under ``/var/tmp`` in the watchdog container's writable layer, so ANY
recreation discarded them — including the deploy that finally wires the transport, which is
precisely when the parked backlog matters. They now live on a named volume, and the script
derives every ledger path from one state root so the mount covers all four at once.
"""

import re
from pathlib import Path

import yaml

_DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
_COMPOSE = _DEPLOY / "docker-compose.yml"
_WATCHDOG = _DEPLOY / "watchdog.sh"

_SERVICE = "teatree-watchdog"
_VOLUME = "teatree_watchdog_state"
_STATE_DIR_VAR = "TEATREE_WATCHDOG_STATE_DIR"

#: Every ledger the watchdog carries across passes; each must sit under the mounted root.
_LEDGER_VARS = (
    "UNDELIVERED_STATE",
    "RED_STATE",
    "LIVENESS_STATE",
    "DEPLOY_PENDING_STATE",
)


def _compose() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def _state_dir() -> str:
    return _compose()["services"][_SERVICE]["environment"][_STATE_DIR_VAR]


class TestTheLedgerRootIsAVolume:
    def test_the_volume_is_declared(self) -> None:
        assert _VOLUME in _compose()["volumes"], f"{_VOLUME} must be a named volume, not a writable layer"

    def test_the_watchdog_mounts_it_at_its_declared_state_dir(self) -> None:
        mounts = _compose()["services"][_SERVICE]["volumes"]

        assert f"{_VOLUME}:{_state_dir()}" in mounts, (
            f"{_SERVICE} must mount {_VOLUME} at the path {_STATE_DIR_VAR} names, or the ledgers "
            "are discarded on every recreation"
        )

    def test_the_state_dir_is_not_the_containers_temp_root(self) -> None:
        # The anti-vacuous control: pointing the var back at /var/tmp would satisfy the two
        # assertions above while restoring the exact defect.
        assert not _state_dir().startswith("/var/tmp"), "a temp root is swept; it cannot hold a backlog"
        assert not _state_dir().startswith("/tmp"), "a temp root is swept; it cannot hold a backlog"


class TestTheScriptDerivesEveryLedgerFromOneRoot:
    def test_the_state_root_reads_the_compose_variable(self) -> None:
        script = _WATCHDOG.read_text(encoding="utf-8")

        assert re.search(rf'WATCHDOG_STATE_DIR="\$\{{{_STATE_DIR_VAR}:-', script), (
            f"the script must take its state root from {_STATE_DIR_VAR}"
        )

    def test_every_ledger_default_sits_under_the_state_root(self) -> None:
        script = _WATCHDOG.read_text(encoding="utf-8")

        for var in _LEDGER_VARS:
            assignment = re.search(rf"^{var}=.*$", script, re.MULTILINE)
            assert assignment is not None, f"{var} must still be defined"
            assert "$WATCHDOG_STATE_DIR" in assignment.group(0), (
                f"{var} must default under the mounted state root, not a hard-coded temp path"
            )
