r"""A shell operator in a Dockerfile ``RUN`` chain must reach the shell unescaped.

``\&\&`` still parses — the shell reads it as two literal ``&`` arguments to the
previous command — so neither ``sh -n`` nor the CLI-pin tests (which only match
version strings) notice that ``npm install -g … \&\& \`` hands ``&&`` to npm and
the image never builds.
"""

# test-path: cross-cutting — pins the repo's Dockerfiles; no teatree module owns them.

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_DOCKERFILES = sorted([_ROOT / "deploy" / "Dockerfile", *(_ROOT / "dev").glob("Dockerfile*")])


def test_every_dockerfile_is_found() -> None:
    assert len(_DOCKERFILES) >= 2, _DOCKERFILES


@pytest.mark.parametrize("dockerfile", _DOCKERFILES, ids=lambda path: path.relative_to(_ROOT).as_posix())
def test_no_run_line_escapes_a_shell_operator(dockerfile: Path) -> None:
    escaped = [
        f"{dockerfile.name}:{number}: {line.strip()}"
        for number, line in enumerate(dockerfile.read_text(encoding="utf-8").splitlines(), start=1)
        if "\\&" in line or "\\|" in line
    ]
    assert not escaped, escaped
