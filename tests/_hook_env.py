"""The hook environment's name, derived exactly as ``scripts/hooks/lib/resolve-uv.sh`` does.

One source of truth for the four suites that assert on it, so a test can never
agree with a stale spelling the shell no longer produces.
"""

import platform

HOOK_ENV_NAME = f".venv-hook-{platform.system().lower()}-{platform.machine()}"
