"""The ORM half of the host-projection write seam.

:mod:`teatree.config.cold_writer` republishes after a Django-free write; these
receivers republish after an ORM one, so BOTH ways of writing a projected row
regenerate the projection. There is no timer anywhere — a projection republished by
anything other than the thing that changed the row is a projection that silently
goes stale.

Republication rides ``transaction.on_commit`` because the projection is read from the
database rather than from the instance: publishing mid-transaction would hand the
host rows a rollback could still take back.
"""

import logging
from collections.abc import Callable
from pathlib import Path

from django.db import connections, models, router, transaction
from django.db.models.signals import ModelSignal, post_delete, post_save

from teatree.config.cold_db import projection_dir_for
from teatree.config.host_projection import GENERATION_KEY, GLOBAL_SCOPE, ProjectionPublisher, next_generation
from teatree.core.models.config_setting import ConfigSetting
from teatree.core.models.loop_preset import Mode, ModeOverride
from teatree.core.models.loop_schedule import ModeSchedule, ModeScheduleSlot
from teatree.core.models.loop_state import LoopState
from teatree.db.boundary import ControlDbBoundary

logger = logging.getLogger(__name__)

#: Every model the projection carries rows of, apart from ``ConfigSetting`` — which
#: needs its own receiver to skip the generation row it would otherwise recurse on.
#: The four mode tables are here because the Django-free posture resolver walks them
#: on a host, where the database itself cannot be opened.
_PROJECTED_MODELS: tuple[type[models.Model], ...] = (LoopState, Mode, ModeOverride, ModeSchedule, ModeScheduleSlot)

_WRITE_SIGNALS: tuple[tuple[ModelSignal, str], ...] = ((post_save, "save"), (post_delete, "delete"))


def control_db_path() -> Path:
    """The file the ORM writes ``ConfigSetting`` to — the router's answer, not a guess.

    From a worktree the ``ConfigSettingRouter`` pins config onto the primary DB, so
    reading the alias back is the only way the projection can be built from the
    database the rows actually landed in.
    """
    alias = router.db_for_write(ConfigSetting) or "default"
    return Path(str(connections[alias].settings_dict["NAME"]))


def projects_to_a_host() -> bool:
    """Whether this connection's database is a real file a projection can be built from.

    An in-memory database has no host side to project to, no second connection can read
    it, and no cold hook will ever consult it — so the whole seam is inapplicable rather
    than merely failing. Uses the boundary's own file-backed predicate so "is this a real
    file" has one definition.
    """
    return ControlDbBoundary(control_db_path()).file_backed


def publish_projection_now() -> None:
    """Publish from the database the ORM is actually writing; loud, but never fatal.

    The row is already committed by the time this runs, so raising would report a
    write that landed as a write that did not. ``t3 doctor check`` is the paired
    detector: it runs inside the container, where source and projection are both
    visible, and compares their generations.
    """
    db_path = control_db_path()
    try:
        ProjectionPublisher(db_path, projection_dir_for(db_path)).publish()
    except Exception:
        logger.exception("The host projection was not republished; host hooks may be serving stale values")


def _ratchet_and_republish() -> None:
    stored = ConfigSetting.objects.filter(scope=GLOBAL_SCOPE, key=GENERATION_KEY).values_list("value", flat=True)
    ConfigSetting.objects.update_or_create(
        scope=GLOBAL_SCOPE,
        key=GENERATION_KEY,
        defaults={"value": next_generation(next(iter(stored), None))},
    )
    transaction.on_commit(publish_projection_now)


def _republish_on_config_setting_write(
    sender: type,  # noqa: ARG001 — Django signal receiver signature requires sender even when unused
    instance: ConfigSetting,
    **kwargs: object,  # noqa: ARG001 — Django sends created/raw/using/update_fields this receiver ignores
) -> None:
    # The generation row is written BY the ratchet below, so republishing on it would
    # recurse; it also carries no operator intent of its own.
    if instance.key == GENERATION_KEY or not projects_to_a_host():
        return
    _ratchet_and_republish()


def _republish_on_projected_write(
    sender: type,  # noqa: ARG001 — Django signal receiver signature requires sender even when unused
    instance: models.Model,  # noqa: ARG001 — the projection is rebuilt from the table, not from the instance
    **kwargs: object,  # noqa: ARG001 — Django sends created/raw/using/update_fields this receiver ignores
) -> None:
    if projects_to_a_host():
        _ratchet_and_republish()


def _connect(model: type[models.Model], receiver: Callable[..., None]) -> None:
    """Wire *receiver* to *model*'s save AND delete, under a uid unique to that pair."""
    for signal, verb in _WRITE_SIGNALS:
        signal.connect(receiver, sender=model, dispatch_uid=f"host_projection_{model.__name__}_{verb}")


def register_projection_signals() -> None:
    _connect(ConfigSetting, _republish_on_config_setting_write)
    for model in _PROJECTED_MODELS:
        _connect(model, _republish_on_projected_write)
