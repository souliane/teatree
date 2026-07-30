# test-path: cross-cutting
"""Every remote fetch in the deploy image is attributable and retried.

The box's convergence failed on `docker compose up -d --build` with a bare
`/bin/sh: 1: npm: not found` (exit 127). The cause was 15 seconds and one masked
error earlier: `curl -fsSL https://deb.nodesource.com/setup_24.x | bash -` got
HTTP 403, and because Docker's default shell is dash — which has no `pipefail` —
the pipeline reported the shell's success, the `&&` chain continued, the
NodeSource repo was never added, and `apt-get install nodejs` installed Ubuntu's
nodejs 18, which packages npm separately.

Three properties keep that from recurring silently: the build shell has
`pipefail`, no fetch is piped straight into a shell, and a transient CDN error is
retried before it can fail a deploy.
"""

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "deploy" / "Dockerfile"


def _content() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _code_lines() -> list[str]:
    return [line for line in _content().splitlines() if not line.lstrip().startswith("#")]


def test_build_shell_enables_pipefail() -> None:
    shell_lines = [line for line in _code_lines() if line.startswith("SHELL ")]
    assert shell_lines, (
        "deploy/Dockerfile must declare a SHELL — the default dash has no `pipefail`, so a "
        "failed fetch inside a pipeline is invisible to the `&&` chain."
    )
    assert any("pipefail" in line and "bash" in line for line in shell_lines), (
        f"the SHELL directive must be bash with -o pipefail; got {shell_lines!r}"
    )


def test_no_remote_fetch_is_piped_into_a_shell() -> None:
    # `curl ... | bash` also feeds a truncated or error body to a shell even when
    # pipefail catches the exit status, so the pattern is banned outright.
    piped = [line for line in _code_lines() if "curl" in line and re.search(r"\|\s*(ba)?sh\b", line)]
    assert not piped, f"fetch to a file and run it, never pipe into a shell: {piped!r}"


def test_the_nodesource_fetch_is_retried_and_fails_loud_when_empty() -> None:
    content = _content()
    assert "deb.nodesource.com/setup_24.x -o " in content, "the NodeSource setup script must be downloaded to a file"
    assert "-s /tmp/nodesource_setup.sh" in content, (
        "an empty/failed NodeSource download must fail the build immediately, at the point "
        "where the cause is still nameable — not 15 seconds later as `npm: not found`."
    )
    assert re.search(r"for attempt in .*?; do.*?nodesource", content, re.DOTALL), (
        "a transient CDN 403/5xx must be retried, not fail the whole deploy"
    )


def test_npm_absence_is_asserted_before_it_is_used() -> None:
    code = "\n".join(_code_lines())
    npm_guard = code.find("command -v npm")
    npm_install = code.find("npm install -g")
    assert npm_guard != -1, "the image must assert npm exists before installing global packages"
    assert npm_guard < npm_install, "the assertion must precede the use it protects"


def test_the_uv_installer_fetch_is_hardened_too() -> None:
    # Same class, second call site: this was the image's other `curl | shell`.
    content = _content()
    assert "astral.sh/uv/install.sh -o " in content
    assert "-s /tmp/uv-install.sh" in content
