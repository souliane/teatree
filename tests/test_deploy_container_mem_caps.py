"""Container memory caps in ``deploy/docker-compose.yml`` are sized from measured need (#3651).

``mem_limit`` is a cgroup CEILING, not a reservation: an unused cap costs nothing,
so a cap must cover the container's realistic CONCURRENT peak with headroom. These
tests pin the measured floors so a future shrink fails loudly rather than silently
reintroducing an OOM kill.

The sharp case is the admin: it is not the lean gunicorn-only server its old 512m
cap assumed. It is the default ``docker exec`` target for CLI work AND the target
of the watchdog's health probe, so it routinely runs a Django-booting CLI command
(measured ~380 MiB) alongside gunicorn serving the dashboard (measured ~257 MiB).
Under 512m a routine ``config_setting set`` was OOM-killed.
"""

# test-path: cross-cutting -- pins deploy/docker-compose.yml caps against src/teatree/utils/ram_probe.py
from pathlib import Path

import yaml

from teatree.utils.ram_probe import _SIBLING_RESERVE_MIB, derive_worker_mem_limit_mib

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "deploy" / "docker-compose.yml"

_MIB = 1024**2
_GIB = 1024**3

# Measured on the live 30 GiB box, idle unless noted.
_ADMIN_GUNICORN_IDLE = 257 * _MIB
_ADMIN_HEALTH_PROBE = 380 * _MIB
_SLACK_LISTENER_OBSERVED = 264 * _MIB
_WATCHDOG_OBSERVED = 61 * _MIB

_SIBLING_SERVICES = ("teatree-admin", "teatree-slack-listener", "teatree-watchdog")

_SUFFIX_BYTES = {"b": 1, "k": 1024, "m": _MIB, "g": _GIB}


def _parse_mem(value: str) -> int:
    """Bytes for a compose ``mem_limit`` scalar (``512m``, ``2g``, ``1073741824``)."""
    text = str(value).strip().strip('"').lower().removesuffix("b")
    if text[-1] in _SUFFIX_BYTES:
        return int(text[:-1]) * _SUFFIX_BYTES[text[-1]]
    return int(text)


def _services() -> dict[str, dict]:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))["services"]


def _cap(service: str) -> int:
    return _parse_mem(_services()[service]["mem_limit"])


class TestAdminCapHoldsWebServerPlusCli:
    def test_cap_covers_gunicorn_and_a_concurrent_django_cli(self) -> None:
        # The concurrency that OOM-killed under 512m: dashboard + a Django-booting CLI.
        assert _cap("teatree-admin") >= _ADMIN_GUNICORN_IDLE + _ADMIN_HEALTH_PROBE

    def test_cap_leaves_headroom_for_a_second_concurrent_cli(self) -> None:
        # The watchdog probes every pass while an operator exec is already running.
        assert _cap("teatree-admin") >= _ADMIN_GUNICORN_IDLE + 2 * _ADMIN_HEALTH_PROBE


class TestSiblingCapsLeaveBurstRoom:
    """Both sat near 50% of their old caps at idle — too little room for a spike."""

    def test_slack_listener_leaves_triple_its_observed_usage(self) -> None:
        assert _cap("teatree-slack-listener") >= 3 * _SLACK_LISTENER_OBSERVED

    def test_watchdog_leaves_triple_its_observed_usage(self) -> None:
        assert _cap("teatree-watchdog") >= 3 * _WATCHDOG_OBSERVED


class TestWorkerKeepsBurstCeiling:
    def test_worker_default_stays_a_generous_ceiling(self) -> None:
        # The worker hosts headless agents and their test suites — the box was
        # observed at 27/30 GiB under load. Its fallback stays far above the
        # siblings; the deploy-derived value normally supersedes it.
        raw = _services()["teatree-worker"]["mem_limit"]
        fallback = _parse_mem(raw.split(":-")[1].rstrip("}"))
        assert fallback >= 16 * _GIB
        assert fallback > _cap("teatree-admin")


class TestSiblingReserveMatchesTheDeclaredCaps:
    def test_worker_sizing_reserves_what_the_siblings_may_actually_take(self) -> None:
        # ram_probe carves a fixed sibling reserve off host RAM before sizing the
        # worker. A reserve below the siblings' own ceilings is a stale reference.
        siblings_mib = sum(_cap(name) for name in _SIBLING_SERVICES) // _MIB
        assert siblings_mib <= _SIBLING_RESERVE_MIB

    def test_derived_worker_ceiling_still_leaves_the_siblings_their_caps(self) -> None:
        host_mib = 32000
        derived_mib = derive_worker_mem_limit_mib(total_ram_mib=host_mib)
        siblings_mib = sum(_cap(name) for name in _SIBLING_SERVICES) // _MIB
        assert derived_mib + siblings_mib <= host_mib


class TestCeilingNotReservationIsDocumented:
    def test_compose_states_the_sizing_principle(self) -> None:
        text = COMPOSE_FILE.read_text(encoding="utf-8").lower()
        assert "ceiling, not a reservation" in text
