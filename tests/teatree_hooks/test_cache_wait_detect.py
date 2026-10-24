import pytest

from teatree.agents.pydantic_ai_config import ANTHROPIC_CACHE_TTL_SECONDS
from teatree.hooks.cache_wait_detect import CACHE_WAIT_LIMIT_SECONDS, declared_cache_expiring_wait

ORDINARY_COMMANDS = (
    "git status",
    "git diff --stat",
    "git log -5 --oneline",
    "git fetch origin main",
    "git commit -m 'sleep 600 is documented'",
    "git grep 'gh run watch'",
    "git branch sleep-600",
    "gh pr view 42",
    "gh run list",
    "gh run view 123",
    "gh run watch --help",
    "glab ci status",
    "glab ci status --help",
    "glab ci view --watch",
    "pytest -q",
    "uv run pytest tests/test_x.py",
    "ruff check src",
    "tach check",
    "sleep 5",
    "sleep 239",
    "sleep 3m",
    "sleep 0.5m",
    "echo 'sleep 600'",
    'printf "gh run watch 123"',
    "rg 'glab ci status --watch' docs",
    "cat README.md",
    "cat <<'EOF'\nsleep 600\nEOF",
    "python -c 'print(\"sleep 600\")'",
    "bash -c 'echo sleep 600'",
    "timeout 900 pytest -q",
    "timeout 5m bash -c 'pytest -q'",
    "docker compose up -d",
    "docker compose logs --tail 50",
    "t3 teatree test run",
    "t3 teatree workspace status",
    "t3 tool verify-gates",
    "make test",
    "npm test",
    "cargo test",
    "cd src && git status",
    "echo done && sleep 10",
    "# sleep 600\ngit status",
    "cat > note.txt <<'EOF'\ngh run watch 123\nEOF",
    "env LIMIT=600 pytest -q",
    "sleep 300 &",
    "gh run watch 123 & git status",
    "timeout 5 sleep 300",
    "timeout 1m gh run watch 123",
)


@pytest.mark.parametrize("command", ORDINARY_COMMANDS)
def test_ordinary_command_does_not_claim_a_cache_expiring_wait(command: str) -> None:
    assert declared_cache_expiring_wait(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "sleep 300",
        "sleep 5m",
        "sleep 4m",
        "cd src && sleep 600",
        "bash -c 'sleep 300'",
        "gh run watch 123",
        "glab ci status --watch",
    ],
)
def test_declared_foreground_wait_is_reported(command: str) -> None:
    assert declared_cache_expiring_wait(command) is not None


def test_wait_threshold_is_below_the_native_anthropic_cache_lifetime() -> None:
    assert CACHE_WAIT_LIMIT_SECONDS < ANTHROPIC_CACHE_TTL_SECONDS
