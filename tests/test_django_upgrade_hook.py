"""Pins the ``django-upgrade`` pre-commit hook wiring.

``django-upgrade`` (adamchainz/django-upgrade) is a codemod that auto-rewrites
deprecated Django patterns to the idioms of a target Django version. It is wired
as a prek/pre-commit hook so every new Python file is upgraded on commit.

These tests pin three invariants so a future edit can't silently break the hook:

- the hook with id ``django-upgrade`` is present in ``.pre-commit-config.yaml``;
- it pins a concrete upstream ``rev`` (never a floating branch);
- its ``--target-version`` equals the *minimum* supported Django version parsed
    from ``pyproject.toml`` — the floor, so rewrites stay compatible with every
    Django the project supports.
"""

import re
import tomllib
from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PRECOMMIT_CONFIG = _REPO_ROOT / ".pre-commit-config.yaml"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _load_precommit_repos() -> list[dict[str, Any]]:
    config = yaml.safe_load(_PRECOMMIT_CONFIG.read_text(encoding="utf-8"))
    return cast("list[dict[str, Any]]", config["repos"])


def _django_upgrade_repo() -> dict[str, Any] | None:
    for repo in _load_precommit_repos():
        if any(hook.get("id") == "django-upgrade" for hook in repo.get("hooks", [])):
            return repo
    return None


def _django_floor_version() -> str:
    """The minimum supported Django version from the ``django>=X.Y`` pin."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    deps: list[str] = data["project"]["dependencies"]
    django_pin = next(dep for dep in deps if re.match(r"^django\b", dep))
    match = re.search(r">=\s*(\d+\.\d+)", django_pin)
    assert match, f"Could not parse a '>=X.Y' floor from the Django pin: {django_pin!r}"
    return match.group(1)


class TestDjangoUpgradeHook:
    def test_hook_is_wired(self) -> None:
        repo = _django_upgrade_repo()
        assert repo is not None, "No repo wiring a 'django-upgrade' hook in .pre-commit-config.yaml."
        assert "adamchainz/django-upgrade" in repo["repo"], (
            "The django-upgrade hook must come from adamchainz/django-upgrade."
        )

    def test_rev_is_pinned(self) -> None:
        repo = _django_upgrade_repo()
        assert repo is not None
        rev = str(repo.get("rev", "")).strip()
        assert rev, "django-upgrade must pin a concrete release tag."
        assert rev not in {"main", "master", "HEAD"}, (
            f"django-upgrade must pin a concrete release tag, not a floating ref: {rev!r}."
        )

    def test_target_version_matches_django_floor(self) -> None:
        repo = _django_upgrade_repo()
        assert repo is not None
        hook = next(h for h in repo["hooks"] if h.get("id") == "django-upgrade")
        args = [str(arg) for arg in hook.get("args", [])]
        assert "--target-version" in args, (
            "django-upgrade must pass an explicit --target-version so rewrites are "
            "deterministic and compatible with the supported floor."
        )
        target = args[args.index("--target-version") + 1]
        floor = _django_floor_version()
        assert target == floor, (
            f"django-upgrade --target-version ({target!r}) must equal the minimum "
            f"supported Django version from pyproject ({floor!r}); rewrites keyed to a "
            "higher target can emit code that breaks on the supported floor."
        )
