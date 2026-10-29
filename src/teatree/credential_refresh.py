"""Live OAuth health reads at the credential dispatch boundary."""

import datetime as dt
import logging
from dataclasses import dataclass

from teatree.core.models.anthropic_token_usage import AnthropicTokenUsage, TokenHealthReading
from teatree.credential_readings import reading_from
from teatree.llm.rate_limits import RateLimitProbeError, RateLimitReader
from teatree.utils.secrets import SecretReader, SecretStoreError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountProber:
    reader: RateLimitReader
    secret_reader: SecretReader

    def read(self, pass_path: str) -> TokenHealthReading | None:
        try:
            token = self.secret_reader(pass_path)
        except SecretStoreError:
            logger.warning("OAuth health: the secret store could not be read for %s", pass_path)
            return None
        if not token:
            return None
        try:
            return reading_from(self.reader(token, is_oauth=True))
        except RateLimitProbeError:
            logger.warning("OAuth health: probing %s failed", pass_path)
            return None


def refresh_oauth_readings(paths: list[str], *, now: dt.datetime, prober: AccountProber) -> None:
    rows = {row.pass_path: row for row in AnthropicTokenUsage.objects.filter(pass_path__in=paths)}
    for path in paths:
        row = rows.get(path)
        if row is not None and row.is_fresh(now):
            continue
        reading = prober.read(path)
        if reading is not None:
            AnthropicTokenUsage.objects.record(path, reading, now=now)
