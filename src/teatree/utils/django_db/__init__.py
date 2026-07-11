"""Generic Django database provisioning engine (package facade).

The reference-DB + template-copy import engine, formerly a single
``utils/django_db.py`` god-module, split by concern into cohesive
submodules while keeping ``teatree.utils.django_db`` a stable import path:

- :mod:`~teatree.utils.django_db.config` — the ``DjangoDbImportConfig`` knob.
- :mod:`~teatree.utils.django_db.runner` — the manage.py interpreter-prefix chokepoint.
- :mod:`~teatree.utils.django_db.helpers` — stateless Postgres primitives.
- :mod:`~teatree.utils.django_db.restore` — dump-source validation.
- :mod:`~teatree.utils.django_db.migrate` — migrate-outcome vocabulary.
- :mod:`~teatree.utils.django_db.importer` — the ``DjangoDbImporter`` orchestrator.
- :mod:`~teatree.utils.django_db.dslr` — DSLR snapshot primitives.
- :mod:`~teatree.utils.django_db.reconcile` — renumbered-migration reconcile.
- :mod:`~teatree.utils.django_db.snapshot_warmer` — out-of-band snapshot refresh.
"""

from teatree.utils.django_db.config import DjangoDbImportConfig
from teatree.utils.django_db.dslr import prune_dslr_snapshots
from teatree.utils.django_db.importer import DjangoDbImporter, django_db_import
from teatree.utils.django_db.restore import validate_dump
from teatree.utils.django_db.runner import runner_prefix

__all__ = [
    "DjangoDbImportConfig",
    "DjangoDbImporter",
    "django_db_import",
    "prune_dslr_snapshots",
    "runner_prefix",
    "validate_dump",
]
