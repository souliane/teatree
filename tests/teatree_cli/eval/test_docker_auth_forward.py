"""The one-credential-var forward into the metered eval container.

``_forwarded_container_credential`` recovers the container's credential KIND by
sniffing which Anthropic var is present in its env, with no knob to tell it. That
inference is only sound while the host forwards exactly ONE such var — forwarding
a second would silently flip the container onto the other credential.

The invariant is documented at both ends but lives in two modules, so nothing
connected them. These tests are that connection: they fail if the docker lane ever
forwards more than one Anthropic credential var.
"""

from pathlib import Path
from unittest.mock import patch

from teatree.cli.eval.docker import _auth_passthrough_flags, run_eval_in_docker
from teatree.llm.credentials import AnthropicApiKeyCredential, AnthropicSubscriptionCredential

_ANTHROPIC_CREDENTIAL_VARS = frozenset(
    {
        AnthropicApiKeyCredential.spec.env_var,
        AnthropicSubscriptionCredential.spec.env_var,
    }
)


class TestAuthPassthroughFlags:
    """``-e VARNAME`` carries the NAME only — docker reads the value from the host env."""

    def test_forwards_a_present_var_by_name_never_by_value(self, monkeypatch) -> None:
        var = AnthropicSubscriptionCredential.spec.env_var
        monkeypatch.setenv(var, "sk-ant-oat01-secret-value")

        flags = _auth_passthrough_flags((var,))

        assert flags == ["-e", var]
        assert "sk-ant-oat01-secret-value" not in flags, "the secret must never reach argv"

    def test_omits_a_var_absent_from_the_host_env(self, monkeypatch) -> None:
        var = AnthropicApiKeyCredential.spec.env_var
        monkeypatch.delenv(var, raising=False)

        assert _auth_passthrough_flags((var,)) == []


class TestApiLaneForwardsExactlyOneCredentialVar:
    """The invariant `_forwarded_container_credential`'s kind-sniffing depends on."""

    def _forwarded_vars(self, credential_class: type) -> tuple[str, ...]:
        credential = credential_class()
        seen: dict[str, tuple[str, ...]] = {}

        def _capture(_root: Path, _args: list[str], **kwargs: object) -> int:
            seen["vars"] = kwargs.get("auth_env_vars", ())  # type: ignore[assignment]
            return 0

        with (
            patch("teatree.cli.eval.docker.shutil.which", return_value="/usr/bin/docker"),
            patch("teatree.cli.eval.docker._requests_api_lane", return_value=True),
            patch("teatree.cli.eval.docker._image_present", return_value=True),
            patch("teatree.cli.eval.docker._repo_root", return_value=Path("/repo")),
            patch("teatree.cli.eval.docker.ensure_django"),
            patch("teatree.credential_config.resolve_eval_credential", return_value=credential),
            patch.object(credential, "export"),
            patch("teatree.cli.eval.docker._run_in_image", side_effect=_capture),
        ):
            run_eval_in_docker(["--backend", "api"])
        return seen["vars"]

    def test_subscription_lane_forwards_exactly_one_anthropic_var(self) -> None:
        forwarded = self._forwarded_vars(AnthropicSubscriptionCredential)

        assert len(forwarded) == 1
        assert forwarded == (AnthropicSubscriptionCredential.spec.env_var,)

    def test_metered_lane_forwards_exactly_one_anthropic_var(self) -> None:
        forwarded = self._forwarded_vars(AnthropicApiKeyCredential)

        assert len(forwarded) == 1
        assert forwarded == (AnthropicApiKeyCredential.spec.env_var,)

    def test_no_lane_ever_forwards_both_credential_kinds(self) -> None:
        """Two vars in the container would make the kind-sniff pick the wrong credential."""
        for credential_class in (AnthropicSubscriptionCredential, AnthropicApiKeyCredential):
            forwarded = self._forwarded_vars(credential_class)

            assert len(_ANTHROPIC_CREDENTIAL_VARS.intersection(forwarded)) == 1


class TestNonApiLaneForwardsNothing:
    def test_transcript_lane_touches_no_credential(self) -> None:
        """The default backend grades a recorded run, so the secret store stays untouched."""
        seen: dict[str, tuple[str, ...]] = {}

        def _capture(_root: Path, _args: list[str], **kwargs: object) -> int:
            seen["vars"] = kwargs.get("auth_env_vars", ())  # type: ignore[assignment]
            return 0

        with (
            patch("teatree.cli.eval.docker.shutil.which", return_value="/usr/bin/docker"),
            patch("teatree.cli.eval.docker._requests_api_lane", return_value=False),
            patch("teatree.cli.eval.docker._image_present", return_value=True),
            patch("teatree.cli.eval.docker._repo_root", return_value=Path("/repo")),
            patch("teatree.cli.eval.docker._run_in_image", side_effect=_capture),
        ):
            run_eval_in_docker(["--backend", "transcript"])

        assert seen["vars"] == ()
