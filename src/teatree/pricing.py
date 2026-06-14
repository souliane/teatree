"""Anthropic fixed cache-pricing multipliers — the single source of truth.

The cache multipliers are a property of the Anthropic API, not of any model:
relative to a model's base input rate, uncached input bills 1.00x, a cache
*read* bills 0.10x, and a 5-minute cache *write* bills 1.25x. Every cost path
(per-attempt SDK cost, the benchmark warm-equivalent fit, the billed-input
regressor) imports these constants from here so the rates can never drift.
"""

#: A cache *read* bills 0.1x the model's base input rate.
CACHE_READ_MULTIPLIER = 0.1

#: A 5-minute cache *write* bills 1.25x the model's base input rate.
CACHE_WRITE_MULTIPLIER = 1.25
