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
- :mod:`~teatree.utils.django_db.dslr_prune` — DSLR snapshot retention (stale selection + delete).
- :mod:`~teatree.utils.django_db.snapshot_warmer` — out-of-band snapshot refresh.
- :mod:`~teatree.utils.django_db.testdb_clone` — clone the app DB into the test DB, re-cloning on drift (#3326).
"""

from teatree.utils.django_db.config import DjangoDbImportConfig
from teatree.utils.django_db.dslr_prune import prune_dslr_snapshots, stale_dslr_snapshots
from teatree.utils.django_db.helpers import is_loopback_host, rewrite_url_host, url_host
from teatree.utils.django_db.importer import DjangoDbImporter, django_db_import
from teatree.utils.django_db.restore import validate_dump
from teatree.utils.django_db.runner import runner_prefix
from teatree.utils.django_db.testdb_clone import (
    TestDbCloneResult,
    clone_app_db_to_test_db,
    migrations_drifted,
    prepare_test_db,
)

__all__ = [
    "DjangoDbImportConfig",
    "DjangoDbImporter",
    "TestDbCloneResult",
    "clone_app_db_to_test_db",
    "django_db_import",
    "is_loopback_host",
    "migrations_drifted",
    "prepare_test_db",
    "prune_dslr_snapshots",
    "rewrite_url_host",
    "runner_prefix",
    "stale_dslr_snapshots",
    "url_host",
    "validate_dump",
]
