# test-path: cross-cutting
"""The deploy image installs ``ttyd`` for the admin dashboard's debug terminal (#3263).

The dashboard "Debug session" button spawns a loopback ``ttyd`` terminal
(``teatree.agents.terminal_launcher.launch_ttyd``). ttyd was missing from the
deploy image, so the feature 500'd in the Docker deployment. This pins that the
toolchain layer installs it.
"""

from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "deploy" / "Dockerfile"


def test_deploy_dockerfile_installs_ttyd() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")
    apt_install_lines = [line for line in content.splitlines() if "ca-certificates" in line and "curl" in line]
    assert apt_install_lines, "expected the toolchain apt-get install line in deploy/Dockerfile"
    assert any("ttyd" in line for line in apt_install_lines), "deploy/Dockerfile must apt-install ttyd (#3263)"
