import subprocess
import sys


def test_queue_drain_imports_before_django_setup() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import teatree.loop.queue_drain"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
