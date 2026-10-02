"""A ``uv`` stub for hook tests that pin a hermetic PATH.

The shell hooks resolve their interpreter through ``scripts/hooks/lib/resolve-uv.sh``,
whose candidate roots are all ``HOME``- or ``PATH``-relative. A test that pins a
hermetic PATH therefore starves the resolver, and the hook fails CLOSED — correct for a
leak gate, but it stops proving whatever the test was about, and it passes or fails on
where the box happens to keep uv rather than on the behaviour under test.

Pointing ``T3_UV`` at this stub (the override the scanner's own deny message names)
keeps PATH hermetic and the resolution identical everywhere: widening PATH to a real
uv's directory would also hand the gate whatever else lives beside it.
"""

import stat
import sys
from pathlib import Path


def executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def working_uv(path: Path) -> Path:
    """A real-enough ``uv``: answers ``--version`` and executes ``uv run --project``.

    It translates ``uv run [flags] --project <root> python -m <mod> <args>`` into the
    test runner's own interpreter with ``<root>/src`` on ``PYTHONPATH``, which is what
    the hook needs from uv and nothing more — no lockfile resolution, no environment to
    provision, so the scan costs one process on a cold checkout too.
    """
    body = f"""#!/usr/bin/env bash
set -euo pipefail
if [ "${{1:-}}" = "--version" ]; then echo "uv 0.0.0-test"; exit 0; fi
shift                 # run
project=""
while [ $# -gt 0 ]; do
    case "$1" in
        --project) project="$2"; shift 2 ;;
        python) shift; break ;;
        *) shift ;;
    esac
done
exec env "PYTHONPATH=${{project}}/src" {sys.executable} "$@"
"""
    return executable(path, body)
