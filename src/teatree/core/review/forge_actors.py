"""One forge-login classification shared by note and review consumers."""

import re
from dataclasses import dataclass

from teatree.core.self_forge_identities import declared_identities_for_url

_ACCESS_TOKEN_BOT = re.compile(r"(?:^|_)bot(?:_|$)")


@dataclass(frozen=True, slots=True)
class ForgeActors:
    system_logins: frozenset[str] = frozenset()

    @classmethod
    def for_url(cls, url: str) -> "ForgeActors":
        return cls(frozenset(login.strip().casefold() for login in declared_identities_for_url(url) if login.strip()))

    def is_automation(self, login: str, *, user_type: str = "", system: bool = False) -> bool:
        normalized = login.strip().casefold()
        return (
            system
            or not normalized
            or normalized in self.system_logins
            or user_type.casefold() == "bot"
            or normalized.endswith("[bot]")
            or bool(_ACCESS_TOKEN_BOT.search(normalized))
        )
