r"""The pre-eval OAuth selector shim (``scripts/eval/select_oauth.py``).

The freshest-account probe/score is tested in ``tests/teatree_eval/test_oauth_selection.py``;
here the SHIM's env I/O and credential decision are driven with a stubbed
``select_freshest`` (no network) and canned :class:`OAuthSelection` outcomes. The
load-bearing invariant — a token value never reaches stdout except inside an
``::add-mask::`` directive, and a losing token reaches nothing at all — is asserted on
synthetic mock tokens.
"""

import importlib.util
from pathlib import Path

import pytest

from teatree.eval.oauth_selection import CandidateHealth, OAuthSelection, TokenProbeStatus

_SPEC = importlib.util.spec_from_file_location(
    "select_oauth",
    Path(__file__).parents[2] / "scripts" / "eval" / "select_oauth.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

main = _MOD.main

# Synthetic mock tokens — deliberately NOT credential-shaped (no `sk-ant-*` prefix).
TOK_A = "mock-oauth-aaa"
TOK_B = "mock-oauth-bbb"
FALLBACK = "mock-oauth-fallback"


def _healthy(index: int) -> CandidateHealth:
    return CandidateHealth(
        index=index,
        label=f"token[{index + 1}]",
        status=TokenProbeStatus.HEALTHY,
        organization_id="org-mock",
        headroom_5h=0.7,
        headroom_7d=0.6,
    )


def _exhausted(index: int) -> CandidateHealth:
    return CandidateHealth(
        index=index,
        label=f"token[{index + 1}]",
        status=TokenProbeStatus.EXHAUSTED,
        organization_id="org-mock",
        reason="exhausted — 5h 99% used, weekly 10% used",
    )


def _stub_selection(monkeypatch: pytest.MonkeyPatch, outcome: OAuthSelection) -> None:
    monkeypatch.setattr(_MOD, "select_freshest", lambda tokens, **kwargs: outcome)


def _env_lines(env_file: Path) -> list[str]:
    return env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []


class TestFreshestWinnerExported:
    def test_winner_token_written_and_masked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / "gh_env"
        # winner is the SECOND candidate (index 1) → tokens[1] == TOK_B.
        _stub_selection(monkeypatch, OAuthSelection(candidates=(_healthy(0), _healthy(1)), ranked=(_healthy(1),)))
        code = main(
            {
                "EVAL_CREDENTIAL": "subscription_oauth",
                "EVAL_OAUTH_TOKENS": f"{TOK_A}\n{TOK_B}\n",
                "GITHUB_ENV": str(env_file),
            }
        )
        assert code == 0
        lines = _env_lines(env_file)
        assert f"CLAUDE_CODE_OAUTH_TOKEN={TOK_B}" in lines
        assert "T3_AGENT_HARNESS_PROVIDER=subscription_oauth" in lines
        out = capsys.readouterr().out
        assert f"::add-mask::{TOK_B}" in out

    def test_losing_token_leaks_nowhere(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / "gh_env"
        _stub_selection(monkeypatch, OAuthSelection(candidates=(_healthy(0), _healthy(1)), ranked=(_healthy(1),)))
        main(
            {
                "EVAL_CREDENTIAL": "subscription_oauth",
                "EVAL_OAUTH_TOKENS": f"{TOK_A}\n{TOK_B}\n",
                "GITHUB_ENV": str(env_file),
            }
        )
        out = capsys.readouterr().out
        # The losing token (TOK_A) reaches neither stdout nor the exported env file.
        assert TOK_A not in out
        assert TOK_A not in env_file.read_text(encoding="utf-8")

    def test_winner_token_appears_only_inside_add_mask(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / "gh_env"
        _stub_selection(monkeypatch, OAuthSelection(candidates=(_healthy(0),), ranked=(_healthy(0),)))
        main(
            {
                "EVAL_CREDENTIAL": "subscription_oauth",
                "EVAL_OAUTH_TOKENS": TOK_A,
                "GITHUB_ENV": str(env_file),
            }
        )
        out = capsys.readouterr().out
        # Every stdout line carrying the token is the ::add-mask:: masking directive.
        leaking = [line for line in out.splitlines() if TOK_A in line and not line.startswith("::add-mask::")]
        assert leaking == []


class TestBackwardSafePassthrough:
    def test_empty_tokens_probes_the_existing_secret(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / "gh_env"
        _stub_selection(monkeypatch, OAuthSelection(candidates=(_healthy(0),), ranked=(_healthy(0),)))
        code = main(
            {
                "EVAL_CREDENTIAL": "subscription_oauth",
                "EVAL_OAUTH_TOKENS": "",
                "CLAUDE_CODE_OAUTH_TOKEN": FALLBACK,
                "GITHUB_ENV": str(env_file),
            }
        )
        assert code == 0
        lines = _env_lines(env_file)
        assert f"CLAUDE_CODE_OAUTH_TOKEN={FALLBACK}" in lines
        assert f"::add-mask::{FALLBACK}" in capsys.readouterr().out

    def test_single_token_secret_is_probed_before_export(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / "gh_env"
        output_file = tmp_path / "gh_output"
        _stub_selection(monkeypatch, OAuthSelection(candidates=(_exhausted(0),), ranked=()))
        code = main(
            {"CLAUDE_CODE_OAUTH_TOKEN": FALLBACK, "GITHUB_ENV": str(env_file), "GITHUB_OUTPUT": str(output_file)}
        )
        assert code == 75
        assert "parked=true" in _env_lines(output_file)
        assert not any(line.startswith("CLAUDE_CODE_OAUTH_TOKEN=") for line in _env_lines(env_file))

    def test_empty_tokens_and_no_fallback_is_a_clean_noop(self, tmp_path: Path) -> None:
        env_file = tmp_path / "gh_env"
        output_file = tmp_path / "gh_output"
        code = main(
            {
                "EVAL_CREDENTIAL": "subscription_oauth",
                "EVAL_OAUTH_TOKENS": "",
                "GITHUB_ENV": str(env_file),
                "GITHUB_OUTPUT": str(output_file),
            }
        )
        assert code == 0
        assert _env_lines(env_file) == []
        assert not output_file.exists()

    def test_missing_eval_credential_defaults_to_subscription_oauth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / "gh_env"
        _stub_selection(monkeypatch, OAuthSelection(candidates=(_healthy(0),), ranked=(_healthy(0),)))
        code = main({"EVAL_OAUTH_TOKENS": "", "CLAUDE_CODE_OAUTH_TOKEN": FALLBACK, "GITHUB_ENV": str(env_file)})
        assert code == 0
        assert f"CLAUDE_CODE_OAUTH_TOKEN={FALLBACK}" in _env_lines(env_file)


class TestNonOauthCredentialIsNoOp:
    def test_api_key_credential_only_exports_provider(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / "gh_env"
        called: list[object] = []

        def _record(tokens: object) -> OAuthSelection:
            called.append(tokens)
            return OAuthSelection()

        monkeypatch.setattr(_MOD, "select_freshest", _record)
        code = main(
            {"EVAL_CREDENTIAL": "api_key", "EVAL_OAUTH_TOKENS": f"{TOK_A}\n{TOK_B}", "GITHUB_ENV": str(env_file)}
        )
        assert code == 0
        assert called == []  # OAuth selection is never attempted on an api_key run.
        assert "T3_AGENT_HARNESS_PROVIDER=api_key" in _env_lines(env_file)
        assert not any(line.startswith("CLAUDE_CODE_OAUTH_TOKEN=") for line in _env_lines(env_file))


class TestAllExhausted:
    def test_sweep_window_is_forwarded_to_selection(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[tuple[float | None, float, float]] = []

        def select(tokens: list[str], **kwargs: object) -> OAuthSelection:
            reserve = kwargs["reserve"]
            seen.append((kwargs["sweep_within_hours"], reserve.min_5h_free, reserve.min_7d_free))
            return OAuthSelection(candidates=(_exhausted(0),), ranked=())

        monkeypatch.setattr(_MOD, "select_freshest", select)
        code = main(
            {
                "EVAL_OAUTH_TOKENS": TOK_A,
                "EVAL_SWEEP_WITHIN_HOURS": "24",
                "EVAL_RESERVE_5H_FREE": "0.65",
                "EVAL_RESERVE_7D_FREE": "0.55",
                "GITHUB_OUTPUT": str(tmp_path / "out"),
            }
        )

        assert code == 75
        assert seen == [(24.0, 0.65, 0.55)]

    def test_negative_sweep_window_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD, "select_freshest", lambda tokens, **kwargs: pytest.fail("selector was called"))

        with pytest.raises(ValueError, match="EVAL_SWEEP_WITHIN_HOURS must be nonnegative"):
            main({"EVAL_OAUTH_TOKENS": TOK_A, "EVAL_SWEEP_WITHIN_HOURS": "-1"})

    @pytest.mark.parametrize(
        "status", [TokenProbeStatus.BELOW_RESERVE, TokenProbeStatus.UNKNOWN, TokenProbeStatus.UNREACHABLE]
    )
    def test_metered_fallback_requires_every_account_exhausted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: TokenProbeStatus
    ) -> None:
        env_file = tmp_path / "gh_env"
        output_file = tmp_path / "gh_output"
        other = CandidateHealth(index=1, label="token[2]", status=status)
        _stub_selection(monkeypatch, OAuthSelection(candidates=(_exhausted(0), other), ranked=()))
        code = main(
            {
                "EVAL_OAUTH_TOKENS": f"{TOK_A}\n{TOK_B}",
                "EVAL_API_KEY_FALLBACK": "true",
                "GITHUB_ENV": str(env_file),
                "GITHUB_OUTPUT": str(output_file),
            }
        )
        assert code == 75
        assert "parked=true" in _env_lines(output_file)
        assert "T3_AGENT_HARNESS_PROVIDER=api_key" not in _env_lines(env_file)

    def test_parks_when_fallback_off(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / "gh_env"
        output_file = tmp_path / "gh_output"
        _stub_selection(monkeypatch, OAuthSelection(candidates=(_exhausted(0), _exhausted(1)), ranked=()))
        code = main(
            {
                "EVAL_CREDENTIAL": "subscription_oauth",
                "EVAL_OAUTH_TOKENS": f"{TOK_A}\n{TOK_B}",
                "GITHUB_ENV": str(env_file),
                "GITHUB_OUTPUT": str(output_file),
            }
        )
        assert code == 75
        assert "parked=true" in _env_lines(output_file)
        assert not any(line.startswith("CLAUDE_CODE_OAUTH_TOKEN=") for line in _env_lines(env_file))
        out = capsys.readouterr().out
        assert "no eligible OAuth account" in out
        # No token value leaks even in the all-exhausted report.
        assert TOK_A not in out
        assert TOK_B not in out

    def test_flips_to_api_key_when_fallback_opted_in(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / "gh_env"
        _stub_selection(monkeypatch, OAuthSelection(candidates=(_exhausted(0),), ranked=()))
        code = main(
            {
                "EVAL_CREDENTIAL": "subscription_oauth",
                "EVAL_OAUTH_TOKENS": TOK_A,
                "EVAL_API_KEY_FALLBACK": "true",
                "GITHUB_ENV": str(env_file),
            }
        )
        assert code == 0
        assert "T3_AGENT_HARNESS_PROVIDER=api_key" in _env_lines(env_file)
        assert not any(line.startswith("CLAUDE_CODE_OAUTH_TOKEN=") for line in _env_lines(env_file))
        assert "falling back to the metered ANTHROPIC_API_KEY" in capsys.readouterr().out

    def test_fallback_flag_off_values_still_park(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / "gh_env"
        _stub_selection(monkeypatch, OAuthSelection(candidates=(_exhausted(0),), ranked=()))
        code = main(
            {
                "EVAL_CREDENTIAL": "subscription_oauth",
                "EVAL_OAUTH_TOKENS": TOK_A,
                "EVAL_API_KEY_FALLBACK": "0",
                "GITHUB_ENV": str(env_file),
            }
        )
        assert code == 75
