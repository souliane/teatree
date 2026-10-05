"""Fake credentials the artifact-redaction tests plant, and the run environment that names them.

Every fake value is derived at runtime from a seeded hash: no literal secret sits
in the source, and every 16-character window of a fake is distinct, like a real
token's, so a window rule that drops one is caught.
"""

import base64
import hashlib

import pytest

from teatree.eval.artifact_redaction import OAUTH_POOL_ENV


def fake(label: str, seed: int) -> str:
    digest = hashlib.sha256(f"{label}:{seed}".encode()).digest()
    return f"fake-{label}-" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


SUBSCRIPTION = fake("subscription", 1)
API_KEY = fake("apikey", 2)
ROUTER_KEY = fake("router", 3)
POOL = (fake("pool-a", 4), fake("pool-b", 5))
SUFFIXED = fake("deploy", 6)
#: Every HTML/JSON-escapable character and no 16-char word run, so only the
#: escaped-form rule can catch its encoded copies.
SPECIAL = "".join(f"{char}{index}" for index, char in enumerate("\"<>&'\\" * 3))
#: Every character URL encoding rewrites (`/`, `+`, `=`, a space) and no 16-char word
#: run, so only the percent-encoded-form rule can catch its encoded copies.
URL_SPECIAL = "".join(f"{char}{index}" for index, char in enumerate("/+= " * 4))
PASS_ONLY = fake("pass-store", 7)
#: A self-hosted router key can be this short; a named credential is redacted from 8 characters.
SHORT_NAMED = "token-abc123"


def set_fake_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name every fake the way a CI run names its credentials, plus two benign look-alikes."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SUBSCRIPTION)
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", ROUTER_KEY)
    monkeypatch.setenv(OAUTH_POOL_ENV, "\n".join(POOL))
    monkeypatch.setenv("DEPLOY_PASSWORD", SUFFIXED)
    monkeypatch.setenv("SIGNING_SECRET", SPECIAL)
    monkeypatch.setenv("SHORT_TOKEN", "short-benign")
    monkeypatch.setenv("EVAL_LANE_LABEL", "clean_room_lane_label_value")
