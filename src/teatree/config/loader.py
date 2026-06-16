"""TeaTree config loading — ``load_config`` + the toml/logging/dir entry points.

``CONFIG_PATH``, ``load_config`` (builds ``UserSettings`` from ``~/.teatree.toml``),
the toml loader, the default Django LOGGING dict, ``load_e2e_repos``, and the
``workspace_dir`` / ``worktrees_dir`` / ``check_for_updates`` resolvers. Split out
of the package facade for the RUF067 init-is-re-exports-only rule; re-exported
from ``teatree.config`` so every ``teatree.config.<name>`` path stays valid. The
per-setting resolvers live in ``resolution`` and are reached through the package
facade at call-time (the partition's loader -> resolution edge, deferred to avoid
the loader/resolution/discovery import cycle).

``load_config`` builds only the **TOML file tier**, and under the #1775 hard
partition only the TOML-home carve-out reads off it — the global ``[teatree]``
table merged onto the dataclass defaults. Every DB-home field keeps its dataclass
default here; its authoritative value comes from the ``ConfigSetting`` store. The
remaining tiers — env, the DB store (``ConfigSetting`` rows, for DB-home fields),
and the per-overlay ``[overlays.<name>]`` table (for TOML-home fields) — are
layered on top per-field by ``resolution.get_effective_settings`` according to
each field's home; consult its docstring for the per-home precedence. Callers that
need effective values must use ``get_effective_settings``, not the bare
``load_config().user`` (which sees neither env, the DB store, nor per-overlay).
"""

import tomllib
from pathlib import Path

import teatree.config as _facade
from teatree.config.settings import E2ERepo, TeaTreeConfig, UserSettings, _default_handover_mirror_path, _parse_str_list
from teatree.config_mr_reminder import resolve_mr_reminder
from teatree.config_speak import resolve_speak
from teatree.paths import DATA_DIR, get_data_dir
from teatree.update_check import run_update_check

CONFIG_PATH = Path.home() / ".teatree.toml"


def default_logging(namespace: str) -> dict:
    """Return a default Django LOGGING dict that writes to ``<data_dir>/logs/teatree.log``.

    Usage in settings::

        from teatree.config import default_logging
        LOGGING = default_logging("my_overlay")
    """
    log_dir = get_data_dir(namespace) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "verbose": {
                "format": "{asctime} {levelname} {name} {message}",
                "style": "{",
            },
        },
        "handlers": {
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(log_dir / "teatree.log"),
                "maxBytes": 5_000_000,
                "backupCount": 3,
                "formatter": "verbose",
            },
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "verbose",
            },
        },
        "root": {
            "handlers": ["console", "file"],
            "level": "INFO",
        },
        "loggers": {
            "django.request": {"level": "INFO", "propagate": True},
            "teatree": {"level": "DEBUG", "propagate": True},
        },
    }


def _load_toml(path: Path) -> dict:
    """Parse ``path`` as TOML, re-raising a syntax error as a named config error.

    A raw ``tomllib.TOMLDecodeError`` would propagate a parser traceback
    through ``main()`` on every ``t3`` command (even ``--help``); instead it
    becomes a typed, message-bearing ``ValueError`` naming the file and the
    parser's position — the same error shape the intentional invalid-``mode``
    path raises.
    """
    with path.open("rb") as f:
        try:
            return tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            msg = f"Malformed TOML in config file {path}: {exc}"
            raise ValueError(msg) from exc


def load_config(path: Path | None = None) -> TeaTreeConfig:
    if path is None:
        path = _facade.CONFIG_PATH
    if not path.is_file():
        return TeaTreeConfig()

    raw = _load_toml(path)

    teatree = raw.get("teatree", {})
    workspace_dir = Path(teatree.get("workspace_dir", "~/workspace")).expanduser()
    worktrees_dir = Path(teatree.get("worktrees_dir", str(DATA_DIR / "worktrees"))).expanduser()

    # The hard partition (#1775): ``load_config`` builds ONLY the TOML-home fields
    # (the irreducible carve-out — pre-Django readers, path/infra bootstrap,
    # nested structured tables). Every DB-home field keeps its dataclass default
    # at the file tier; its value comes from the ``ConfigSetting`` store via
    # ``get_effective_settings``. ``on_behalf_post_mode`` is DB-home (so it keeps
    # its default here), and ``ask_before_post_on_behalf`` is DERIVED from the
    # mode — derived from the file-tier default mode here, re-derived from the
    # resolved DB-home mode by ``get_effective_settings``. A DB-home key left in
    # ``[teatree]`` / ``[overlays.<name>]`` is ignored on read (its home is the
    # DB); migrate it into the store with ``t3 <overlay> config_setting import``.
    user = UserSettings(
        workspace_dir=workspace_dir,
        worktrees_dir=worktrees_dir,
        privacy=teatree.get("privacy", ""),
        check_updates=teatree.get("check_updates", True),
        timezone=teatree.get("timezone", ""),
        redis_db_count=int(teatree.get("redis_db_count", 16)),
        speak=resolve_speak(teatree),
        mr_reminder=resolve_mr_reminder(raw),
        orchestrator_bash_gate_enabled=bool(teatree.get("orchestrator_bash_gate_enabled", True)),
        statusline_chain=_parse_str_list(teatree["statusline_chain"]) if "statusline_chain" in teatree else [],
        handover_mirror_path=(
            Path(str(teatree["handover_mirror_path"])).expanduser()
            if teatree.get("handover_mirror_path")
            else _default_handover_mirror_path()
        ),
    )

    return TeaTreeConfig(user=user, raw=raw)


def load_e2e_repos(path: Path | None = None) -> list[E2ERepo]:
    """Load named E2E repos from ``[e2e_repos.<name>]`` sections in ``~/.teatree.toml``.

    Each entry may specify ``url``, ``branch``, and optionally ``e2e_dir``
    (the subdirectory containing ``playwright.config.ts``, default ``"e2e"``).
    """
    config = _facade.load_config(path)
    repos = []
    for name, entry in config.raw.get("e2e_repos", {}).items():
        repos.append(
            E2ERepo(
                name=name,
                url=entry.get("url", ""),
                branch=entry.get("branch", "main"),
                e2e_dir=entry.get("e2e_dir", "e2e"),
            )
        )
    return repos


def workspace_dir() -> Path:
    """Canonical workspace directory (where main repo clones live)."""
    from django.conf import settings  # noqa: PLC0415

    if hasattr(settings, "T3_WORKSPACE_DIR"):
        return Path(settings.T3_WORKSPACE_DIR)
    return _facade.load_config().user.workspace_dir


def worktrees_dir() -> Path:
    """Canonical worktrees directory (where ticket worktrees are created)."""
    from django.conf import settings  # noqa: PLC0415

    if hasattr(settings, "T3_WORKTREES_DIR"):
        return Path(settings.T3_WORKTREES_DIR)
    return _facade.load_config().user.worktrees_dir


def check_for_updates(*, force: bool = False) -> str | None:
    """Resolve a "new release available" notice from config + update_check.

    Reads ``check_updates`` from user config and delegates to
    :func:`teatree.update_check.run_update_check`. The implementation
    lives in :mod:`teatree.update_check` (split out for module-health
    LOC); this wrapper is the config-aware entry point.
    """
    return run_update_check(check_updates=_facade.load_config().user.check_updates, force=force)
