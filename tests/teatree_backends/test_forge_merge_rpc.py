"""F8.4 — the keystone merge RPC runners bound every subprocess with a timeout.

An unbounded ``gh``/``glab`` merge call was the one hole left on the KEYSTONE
merge path: a stalled TLS handshake wedged the single-threaded loop indefinitely.
Both runners must thread :data:`_FORGE_MERGE_TIMEOUT_SECONDS` into every
``run_allowed_to_fail`` call.
"""

import subprocess
from unittest.mock import patch

from teatree.backends import forge_merge_rpc as rpc
from teatree.backends.forge_merge_rpc import _FORGE_MERGE_TIMEOUT_SECONDS, gh_runner, glab_runner


def _completed() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, "", "")


def test_gh_runner_threads_the_merge_timeout() -> None:
    with patch.object(rpc, "run_allowed_to_fail", return_value=_completed()) as mock_run:
        gh_runner("tok")(["pr", "view", "9"])
    assert mock_run.call_args.kwargs["timeout"] == _FORGE_MERGE_TIMEOUT_SECONDS


def test_glab_runner_threads_the_merge_timeout() -> None:
    with patch.object(rpc, "run_allowed_to_fail", return_value=_completed()) as mock_run:
        glab_runner()(["api", "projects/1/merge_requests/9"])
    assert mock_run.call_args.kwargs["timeout"] == _FORGE_MERGE_TIMEOUT_SECONDS


def test_gh_runner_passes_token_via_env_and_returns_tuple() -> None:
    with patch.object(rpc, "run_allowed_to_fail", return_value=subprocess.CompletedProcess([], 3, "out", "err")):
        rc, out, err = gh_runner("secret-tok")(["pr", "view", "1"])
    assert (rc, out, err) == (3, "out", "err")


def test_merge_timeout_is_positive_and_finite() -> None:
    assert _FORGE_MERGE_TIMEOUT_SECONDS > 0
