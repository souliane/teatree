import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.core.factory import operational_health
from teatree.core.factory.host_pressure_health import host_pressure_signals
from teatree.core.factory.operational_health import reconcile_health
from teatree.core.models.known_issue import KnownIssue


class TestHostPressureHealth(TestCase):
    def test_missing_malformed_and_stale_feed_share_one_health_row_and_fresh_feed_resolves_it(self) -> None:
        target = Path(self.enterContext(tempfile.TemporaryDirectory())) / "host-pressure.json"
        self.enterContext(patch.dict(os.environ, {"T3_HOST_PRESSURE_PATH": str(target)}))
        with patch.object(operational_health, "_COLLECTORS", (host_pressure_signals,)):
            for contents, fault in [
                (None, "missing"),
                ("{", "malformed"),
                (json.dumps({"epoch": int(time.time()) - 61}), "stale"),
            ]:
                if contents is not None:
                    target.write_text(contents)
                reconcile_health()
                rows = KnownIssue.objects.open().filter(kind="host_pressure_feed")
                assert rows.count() == 1
                assert fault in rows.get().summary
                assert rows.get().severity == KnownIssue.Severity.WARNING

            target.write_text(
                json.dumps(
                    {
                        "epoch": int(time.time()),
                        "cores": 8,
                        "load1": 1.0,
                        "ram_available_mib": 8192,
                    }
                )
            )
            reconcile_health()
        assert not KnownIssue.objects.open().filter(kind="host_pressure_feed").exists()


def test_host_pressure_collector_is_in_the_real_health_roster() -> None:
    assert host_pressure_signals in operational_health._COLLECTORS
