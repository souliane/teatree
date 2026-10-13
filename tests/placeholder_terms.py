"""Neutral, classed term registry for tests that exercise real outbound gates — importable from ``e2e/`` too."""

import json

TEST_TERM_REGISTRY = {
    "leak": ["quartzbridge-bank"],
    "prose_collider": ["meridian-ledger"],
    "overlay": ["quartzbridge-overlay"],
    "allow": ["quartzbridge-bank-engineering"],
}
TEST_TERM_REGISTRY_JSON = json.dumps(TEST_TERM_REGISTRY)
