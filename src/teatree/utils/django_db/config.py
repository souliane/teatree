"""Import-engine configuration surface.

The overlay-supplied :class:`DjangoDbImportConfig` is the only knob the
generic engine reads; everything else (strategy chain, migrate loop, DSLR
plumbing) is derived from it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from subprocess import CompletedProcess

#: Overlay-supplied dockerized migrate runner. Given the ``manage.py`` args and
#: the resolved subprocess env, it runs the migrate inside the repo-canonical
#: docker image (every dependency baked in) and returns the result. Core calls
#: it only as a fallback when the host runner fails with an import/config error
#: — the signature of an unverified, dep-incomplete host venv (#1977).
#:
#: **Forward-key contract (souliane/teatree#3328).** The ``env`` mapping core
#: passes is already *container-oriented*: its ``DATABASE_URL`` host has been
#: rewritten to :attr:`DjangoDbImportConfig.docker_db_host` when that is set, so
#: the implementation must forward it verbatim — never rebuild the URL from a
#: host-side ``POSTGRES_HOST``. The keys the container MUST receive for the
#: migrate to target the intended database are:
#:
#: - ``DATABASE_URL`` — the (already host-rewritten) connection string.
#: - ``DISABLE_DATABASE_SSL`` — core sets this for the local/ref migrate.
#: - every key of ``migrate_env_extra`` — the overlay's settings selectors.
#:
#: Dropping any of these runs the migrate under different settings than core
#: intended (a silently wrong migrate), so an implementation that filters ``env``
#: down to a curated subset must include all of the above.
DockerizedMigrate = Callable[[list[str], dict[str, str]], CompletedProcess[str]]


@dataclass(frozen=True)
class DjangoDbImportConfig:
    ref_db_name: str
    ticket_db_name: str
    main_repo_path: str
    dump_dir: str
    dump_glob: str
    ci_dump_glob: str
    snapshot_tool: str = "dslr"
    remote_db_url: str = ""
    migrate_env_extra: dict[str, str] = field(default_factory=dict)
    dump_timeout: int = 1800
    dslr_snapshot: str = ""
    dump_path: str = ""
    dockerized_migrate: DockerizedMigrate | None = None
    #: The host the container reaches its Postgres at (souliane/teatree#3328). When
    #: set, core rewrites ``DATABASE_URL``'s host to this value before handing the
    #: env to :attr:`dockerized_migrate`, so a URL built from the host-side
    #: ``POSTGRES_HOST`` cannot silently migrate a different database inside the
    #: container. Empty (the default) means no rewrite — byte-identical to the
    #: pre-#3328 behaviour, for an overlay that already shims the host itself.
    docker_db_host: str = ""
