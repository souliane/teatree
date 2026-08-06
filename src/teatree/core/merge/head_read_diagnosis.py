"""Why a live-head read came back EMPTY, and what stays true when it does (#4239).

``fetch_live_head_sha`` returns ``""`` for every failure — no credential, no
network, no such PR — so an empty result is a NON-ANSWER, never a verdict about
the head. The keystone formatted it into a sentence asserting "PR head moved" and
offered two hypotheses that excluded the real one, sending the reader after a
force-push that never happened.

Each chain below is the one that host's merge READ actually authenticates from,
which is neither the PUSH credential ``core.forge_push`` resolves nor the same
across hosts: GitHub shells ``gh`` with an empty token so it inherits the ambient
env (``backends.forge_merge_rpc.gh_runner``), while GitLab retired the ``glab``
binary for an httpx client reading ``GITLAB_TOKEN`` env-first then ``pass``
(#4007). Naming a variable the failing call never reads would repeat this
ticket's own defect inside its fix.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReadCredentialChain:
    """Where a host's merge-read transport looks for its credential."""

    env_vars: tuple[str, ...]
    non_env_source: str


_READ_CREDENTIAL_CHAINS: dict[str, ReadCredentialChain] = {
    "github": ReadCredentialChain(
        env_vars=("GH_TOKEN", "GITHUB_TOKEN"),
        non_env_source="`gh`'s own config file",
    ),
    "gitlab": ReadCredentialChain(
        env_vars=("GITLAB_TOKEN",),
        non_env_source="the `pass` store entry `gitlab/pat`",
    ),
}


def read_credential_chain(host_kind: str) -> ReadCredentialChain:
    """*host_kind*'s merge-read credential chain; GitHub's for an unknown host."""
    return _READ_CREDENTIAL_CHAINS.get(host_kind, _READ_CREDENTIAL_CHAINS["github"])


def read_credential_env_vars(host_kind: str) -> tuple[str, ...]:
    """The env vars *host_kind*'s merge-read transport authenticates from."""
    return read_credential_chain(host_kind).env_vars


def unreadable_head_advisory(host_kind: str) -> str:
    """The likely cause of an empty live-head read in THIS venue, plus the CLEAR's state.

    States what was actually read (which credentials this process carries) rather
    than asserting a cause, and names the non-env fallback an unset variable did
    NOT rule out — so an absent env var reads as evidence, never proof.
    """
    chain = read_credential_chain(host_kind)
    present = [name for name in chain.env_vars if os.environ.get(name, "")]
    cause = (
        f"an ambient credential IS present here ({', '.join(present)}), so a missing credential "
        f"is not the cause — the forge itself did not answer (network, rate limit, a revoked "
        f"token, or no PR of that number in that repo)"
        if present
        else f"this venue carries none of {', '.join(chain.env_vars)}, the credentials the merge "
        f"read transport authenticates from — which is what a `docker compose exec` shell looks "
        f"like, since the entrypoint exports the token into the role process tree only and a "
        f"fresh exec inherits the image env instead. That is evidence, not proof: the transport "
        f"also falls back to {chain.non_env_source}"
    )
    return (
        f"An empty read is a non-answer, not a verdict about the head: {cause}. This refusal "
        f"consumes nothing — the CLEAR stays actionable, so a venue whose forge reads work (the "
        f"authed loop) merges it unchanged on a later tick."
    )
