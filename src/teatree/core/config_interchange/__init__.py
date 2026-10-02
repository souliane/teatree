"""The config store <-> TOML interchange: the export, its inverse, and the rules they share.

``migration`` is the pair itself — ``export_db_to_toml`` and ``import_toml_to_db``, which are
inverses or they are neither. Four modules hold the rules the two directions must agree on by
construction, each carved out so one answer serves both: ``secret_guard`` decides what must
never be shared (of a row, and of a whole file), ``document_layout`` decides which TOML table
holds what, ``registry_rows`` decides how a compound registry row survives being described
incompletely, and ``seed_tables`` carries the ``[loops]`` / ``[modes]`` / ``[schedules]`` half
onto its own rows. ``types`` is what either direction hands back. Imported by submodule path;
no eager re-export (mock.patch targets name the defining submodule).
"""
