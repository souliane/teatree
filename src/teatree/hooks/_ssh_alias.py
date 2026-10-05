"""Bounded, process-cached OpenSSH alias resolution for remote slugs."""

import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from teatree.utils.run import TimeoutExpired, run_bounded_group

#: ``ssh -F`` reads neither default config, so the temporary one includes this after the user's own.
SYSTEM_SSH_CONFIG = Path("/etc/ssh/ssh_config")


def is_canonical_host(host: str) -> bool:
    """Recognize a network host rather than a dotless SSH config alias."""
    return "." in host or host == "localhost"


def ssh_alias_hostname(alias: str) -> str:
    """Ask OpenSSH for the effective hostname without opening a connection."""
    return _cached_ssh_alias_hostname(alias, str(Path.home()))


_CACHE: dict[tuple[str, str], str] = {}
_CACHE_MAXSIZE = 128


def reset_ssh_alias_cache() -> None:
    _CACHE.clear()


def _cached_ssh_alias_hostname(alias: str, _home: str) -> str:
    """Cache successful host resolutions for this process and configuration root."""
    cache_key = (alias, _home)
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    includes = [path for path in (Path(_home) / ".ssh/config", SYSTEM_SSH_CONFIG) if path.is_file()]
    try:
        with TemporaryDirectory() as directory:
            config = Path(directory) / "config"
            config.write_text("".join(f'Include "{path}"\n' for path in includes), encoding="utf-8")
            result = run_bounded_group(
                [shutil.which("ssh") or "/usr/bin/ssh", "-F", str(config), "-G", "--", alias],
                timeout=2,
                expected_codes=None,
            )
    except (OSError, TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        hostname = value.strip().lower()
        if key.lower() == "hostname" and hostname != alias.lower() and is_canonical_host(hostname):
            if len(_CACHE) >= _CACHE_MAXSIZE:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[alias, _home] = hostname
            return hostname
    return ""
