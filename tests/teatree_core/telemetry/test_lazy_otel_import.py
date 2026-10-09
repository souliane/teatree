"""The telemetry package imports without the OpenTelemetry SDK installed.

``core.models.task`` reaches it at module top, and the host hook interpreter has
no ``opentelemetry.sdk``: an eager import made every gate that touches the models
fail closed on ``ModuleNotFoundError`` instead of evaluating the call.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

_BLOCK_OTEL = textwrap.dedent(
    """
    import sys

    class _NoOtel:
        def find_spec(self, name, path=None, target=None):
            if name == "opentelemetry" or name.startswith("opentelemetry."):
                raise ModuleNotFoundError(f"No module named {name!r}")
            return None

    sys.meta_path.insert(0, _NoOtel())
    """
)


def _run(body: str, home: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": str(home), "XDG_DATA_HOME": str(home / "data"), "T3_DATA_DIR": str(home / "t3")}
    return subprocess.run(
        [sys.executable, "-c", _BLOCK_OTEL + textwrap.dedent(body)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_the_admission_module_imports_without_the_sdk(tmp_path: Path) -> None:
    result = _run(
        """
        import teatree.core.telemetry.admission as telemetry
        import teatree.core.telemetry.skill_assurance
        print(telemetry.record_lifecycle_transition.__name__)
        """,
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert "record_lifecycle_transition" in result.stdout


def test_an_emission_without_the_sdk_degrades_to_a_warning(tmp_path: Path) -> None:
    result = _run(
        """
        import teatree.core.telemetry.admission as telemetry
        telemetry.record_lifecycle_transition(kind="task.claimed", entity_id=1)
        print("transition unaffected")
        """,
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert "transition unaffected" in result.stdout
