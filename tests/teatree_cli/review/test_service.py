"""``ReviewService._resolve_base_url`` — where every review post is addressed (#3509).

The address comes from the overlay that owns the service's target repo (#3793). An
explicitly-set ``$GITLAB_URL`` is an operator's stated choice, so it is honoured even
when that overlay read fails — a guarded read that degrades to the env value rather
than refusing. With NO env value there is nothing safe to fall back to, so the read
refuses instead of guessing.
"""

import os
from unittest import mock

import pytest

from teatree.cli.review.guarded_read import ReadRefusedError
from teatree.cli.review.service import ReviewService


def _broken_overlay() -> mock._patch:
    """Patch the repo-derived resolver to raise — the unreadable-overlay case."""
    return mock.patch("teatree.core.overlay_loader.get_overlay", side_effect=RuntimeError("broken overlay"))


class TestResolveBaseUrl:
    def _service(self) -> ReviewService:
        return ReviewService(token="t", repo="acme/widgets")

    def test_an_explicit_env_url_is_honoured_when_the_overlay_read_fails(self) -> None:
        env_url = "https://gitlab.example.com/api/v4"
        with mock.patch.dict(os.environ, {"GITLAB_URL": env_url}), _broken_overlay():
            assert self._service()._resolve_base_url() == env_url

    def test_an_explicit_env_url_is_used_as_the_guarded_neutral(self) -> None:
        # The guarded read succeeds but returns an empty overlay url → the env value
        # is the fallback, never a silent gitlab.com guess.
        env_url = "https://gitlab.example.com/api/v4"
        overlay = mock.Mock()
        overlay.config.gitlab_url = ""
        with (
            mock.patch.dict(os.environ, {"GITLAB_URL": env_url}),
            mock.patch("teatree.core.overlay_loader.get_overlay", return_value=overlay),
        ):
            assert self._service()._resolve_base_url() == env_url

    def test_no_env_and_a_broken_overlay_refuses_rather_than_guessing(self) -> None:
        env = dict(os.environ)
        env.pop("GITLAB_URL", None)
        with (
            mock.patch.dict(os.environ, env, clear=True),
            _broken_overlay(),
            pytest.raises(ReadRefusedError),
        ):
            self._service()._resolve_base_url()
