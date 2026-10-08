import os
from functools import cache

from teatree.utils.ports import running_in_container

CONTAINER_IS_SANDBOX_ENV = "TEATREE_CODEX_CONTAINER_IS_SANDBOX"


@cache
def container_is_the_sandbox() -> bool:
    return os.environ.get(CONTAINER_IS_SANDBOX_ENV) == "1" and running_in_container()
