"""Cold registry imports stay independent of the Pydantic schema."""

import subprocess
import sys
import textwrap


def test_registry_modules_stay_pydantic_free() -> None:
    probe = textwrap.dedent(
        """
        import sys
        import teatree.config.setting_registries
        import teatree.config.registries
        assert "pydantic" not in sys.modules
        assert "teatree.config.schema" not in sys.modules
        print("clean")
        """
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "clean"
