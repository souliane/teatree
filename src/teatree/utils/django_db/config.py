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
