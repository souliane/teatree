"""``ReviewService._resolve_base_url`` — where every review post is addressed (#3509).

An explicitly-set ``$GITLAB_URL`` is an operator's stated choice, so it is honoured
even when the overlay config read fails — a guarded read that degrades to the env
value rather than refusing. With NO env value there is nothing safe to fall back
to, so the read refuses instead of guessing.
"""

import os
from unittest import mock

import pytest

from teatree.cli.review.guarded_read import ReadRefusedError
from teatree.cli.review.service import ReviewService


class TestResolveBaseUrl:
    def test_an_explicit_env_url_is_honoured_when_the_overlay_read_fails(self) -> None:
        env_url = "https://gitlab.example.com/api/v4"
        with (
            mock.patch.dict(os.environ, {"GITLAB_URL": env_url}),
            mock.patch("teatree.core.overlay_loader.get_overlay", side_effect=RuntimeError("broken overlay")),
        ):
            assert ReviewService._resolve_base_url() == env_url

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
            assert ReviewService._resolve_base_url() == env_url

    def test_no_env_and_a_broken_overlay_refuses_rather_than_guessing(self) -> None:
        env = dict(os.environ)
        env.pop("GITLAB_URL", None)
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("teatree.core.overlay_loader.get_overlay", side_effect=RuntimeError("broken overlay")),
            pytest.raises(ReadRefusedError),
        ):
            ReviewService._resolve_base_url()
