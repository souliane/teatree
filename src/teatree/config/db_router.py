"""Pin the ``ConfigSetting`` store to the canonical DB, never a worktree-isolated copy.

:func:`teatree.paths.resolve_data_dir` deliberately auto-isolates a worktree
checkout onto a sibling per-worktree ``db.sqlite3``, so unmerged control-DB
migrations can never reach the DB the installed ``t3`` and the live loop run on.
That isolation is right for RUNTIME state — tasks, sessions, worktree rows and
attempts belong to the worktree that produced them. It is wrong for CONFIG:
``teatree_config_setting`` is operator intent about the whole install, so a
worktree that *reads* a frozen seeded copy silently runs on stale settings, and
one that *writes* an override parks it in a store nothing else ever reads.

The Django-free cold path already encodes the right rule —
:func:`teatree.config.cold_db.canonical_config_db` resolves the PRIMARY
``~/.local/share/teatree/db.sqlite3`` even from inside a worktree. This module is
its ORM twin: a database router that pins the ``ConfigSetting`` model, on READ as
well as WRITE, to a :data:`CONFIG_DB_ALIAS` connection bound to that same file,
so the two tiers can no longer disagree about where config lives.

The alias is registered by the settings module ONLY when the default connection
resolves somewhere else — i.e. an auto-isolated worktree. In a primary clone (and
in an explicit ``XDG_DATA_HOME`` sandbox, which is a deliberate whole-install
sandbox) the default connection already IS the canonical DB, no second alias is
registered, and :meth:`ConfigSettingRouter._pinned_alias` returns ``None``: the
router is inert and every existing entry point, test included, keeps its single
connection.

Migrations never run against the alias — :meth:`ConfigSettingRouter.allow_migrate`
returns ``False`` for it. That is the whole point of the isolation: worktree code
may carry unmerged migrations and none of them may touch the canonical DB. The
isolated ``default`` DB still receives the full schema, including a
``teatree_config_setting`` table that is simply left unused.
"""

import os
from collections.abc import Mapping
from pathlib import Path

from django.db import connections
from django.db.models import Model

from teatree.config.cold_db import canonical_config_db

#: Connection alias holding the canonical config DB, registered by the settings
#: module only when the ``default`` connection points somewhere else.
CONFIG_DB_ALIAS = "canonical_config"

#: ``ConfigSetting._meta.label_lower`` — matched as a string so this platform-layer
#: module never imports the domain-layer model (a backwards tach edge).
CONFIG_MODEL_LABEL = "core.configsetting"


def pinned_config_db(
    *,
    default_db: Path,
    env: Mapping[str, str] = os.environ,
    home: Path | None = None,
) -> Path | None:
    """The canonical config DB when *default_db* is not already it, else ``None``.

    ``None`` means "the default connection is the canonical DB" — the primary
    clone and the explicit-sandbox cases — so no second alias is registered and
    the router stays inert. A path means the caller is running from an
    auto-isolated worktree and config must be routed away from its local DB.

    Both sides are resolved before comparison so a symlinked home or data root
    (``/tmp`` → ``/private/tmp`` on macOS) cannot fake a difference and register
    a redundant second connection onto the very same sqlite file.

    A canonical DB that does not exist on disk also returns ``None``: there is
    no operator config to pin, and registering a connection onto a file in a
    nonexistent directory makes Django's connection setup crash with "unable to
    open database file" (a provisioned test worktree under a fresh ``$HOME``).
    Inert-when-absent, like the rest of this module.
    """
    canonical = canonical_config_db(env=env, home=home)
    if not canonical.is_file():
        return None
    if canonical.expanduser().resolve() == default_db.expanduser().resolve():
        return None
    return canonical


class ConfigSettingRouter:
    """Route ``ConfigSetting`` reads and writes to :data:`CONFIG_DB_ALIAS` when it exists.

    Returning ``None`` from every hook for every other model leaves the rest of
    the control DB exactly where it was — this router only ever moves config.
    """

    def _pinned_alias(self, model: type[Model]) -> str | None:  # noqa: PLR6301 — Django router contract: instance method
        if model._meta.label_lower != CONFIG_MODEL_LABEL:  # noqa: SLF001 — Django's documented Model._meta API
            return None
        return CONFIG_DB_ALIAS if CONFIG_DB_ALIAS in connections.databases else None

    def db_for_read(self, model: type[Model], **hints: object) -> str | None:  # noqa: ARG002 — Django router contract: hints are part of the signature
        return self._pinned_alias(model)

    def db_for_write(self, model: type[Model], **hints: object) -> str | None:  # noqa: ARG002 — Django router contract: hints are part of the signature
        return self._pinned_alias(model)

    def allow_migrate(self, db: str, app_label: str, **hints: object) -> bool | None:  # noqa: ARG002, PLR6301 — Django router contract: instance method; app_label/hints are part of the signature
        """Refuse every migration against the canonical alias; defer for any other.

        Worktree code runs unmerged migrations. Auto-isolation exists so those
        never reach the canonical DB, and routing config there must not punch a
        hole in that guarantee — so the alias is read/write only, never migrated.
        The canonical DB's schema stays owned by the primary clone's
        ``t3 <overlay> db migrate``, exactly as before.
        """
        return False if db == CONFIG_DB_ALIAS else None
