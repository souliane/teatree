"""Every service in ``deploy/docker-compose.yml`` rotates its json-file log.

Docker's default json-file driver never rotates, so a chatty or boot-looping service fills
the disk: the worker log reached 516 MB (+71 MB/day) before anyone read it.
"""

# test-path: cross-cutting -- pins deploy/docker-compose.yml, which has no src/ module
from pathlib import Path

import pytest
import yaml

COMPOSE_FILE = Path(__file__).resolve().parents[1] / "deploy" / "docker-compose.yml"
_SERVICES: dict[str, dict] = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))["services"]


@pytest.mark.parametrize("service", sorted(_SERVICES))
def test_every_service_rotates_its_json_file_log(service: str) -> None:
    logging = _SERVICES[service].get("logging", {})
    assert logging.get("driver") == "json-file"
    assert logging.get("options") == {"max-size": "50m", "max-file": "5"}
