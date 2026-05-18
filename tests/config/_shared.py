"""Shared TOML/manage.py staging helpers for the teatree config test package.

Lifted verbatim from the former monolithic ``tests/test_config.py``
(souliane/teatree#443). No behavior change: the same ``manage.py`` and
``~/.teatree.toml`` writers every focused config test relies on, relocated
so each split module imports them instead of redefining them.
"""

from pathlib import Path


def _write_manage_py(project_path: Path, settings_module: str = "myapp.settings") -> None:
    project_path.mkdir(parents=True, exist_ok=True)
    (project_path / "manage.py").write_text(f'os.environ.setdefault("DJANGO_SETTINGS_MODULE", "{settings_module}")\n')


def _write_toml(config_path: Path, content: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content, encoding="utf-8")
