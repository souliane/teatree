"""The process-wide forge connection pool, sized by the forge_pool_max_connections setting."""

import atexit
from functools import cache

from teatree.config import cold_reader
from teatree.config.settings_provisioning import FORGE_POOL_MAX_CONNECTIONS_MAX, FORGE_POOL_MAX_CONNECTIONS_MIN
from teatree.utils.forge_http import ForgeConnections, ForgeSession


def configured_max_connections() -> int:
    cap = cold_reader.int_setting("forge_pool_max_connections", minimum=FORGE_POOL_MAX_CONNECTIONS_MIN)
    return min(cap, FORGE_POOL_MAX_CONNECTIONS_MAX)


@cache
def forge_connections() -> ForgeConnections:
    return ForgeConnections(max_connections=configured_max_connections())


def client_for_url(url: str) -> ForgeSession:
    return forge_connections().session_for(url)


def close_forge_clients() -> None:
    if forge_connections.cache_info().currsize:
        forge_connections().close()
        forge_connections.cache_clear()


atexit.register(close_forge_clients)
