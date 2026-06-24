from pathlib import Path

from teatree.hooks.own_repo_url_carve_out import term_only_inside_own_repo_urls


def _config(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


class TestTermOnlyInsideOwnRepoUrls:
    def test_term_only_inside_own_gitlab_url_downgrades(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, '[teatree]\nprivate_repos = ["customercorp-engineering"]\n')
        payload = (
            "Tracked the regression against the customer tracker — see "
            "https://gitlab.com/customercorp-engineering/their-svc/-/issues/8223 for context."
        )
        assert term_only_inside_own_repo_urls(payload, "customercorp", config_path=cfg) is True

    def test_bare_term_outside_any_url_still_blocks(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, '[teatree]\nprivate_repos = ["customercorp-engineering"]\n')
        payload = (
            "Rolling out the customercorp integration — see "
            "https://gitlab.com/customercorp-engineering/their-svc/-/issues/8223 for context."
        )
        assert term_only_inside_own_repo_urls(payload, "customercorp", config_path=cfg) is False

    def test_term_inside_foreign_url_still_blocks(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, '[teatree]\nprivate_repos = ["customercorp-engineering"]\n')
        payload = "See https://gitlab.com/customercorp-public/marketing/-/issues/3 for the announcement."
        assert term_only_inside_own_repo_urls(payload, "customercorp", config_path=cfg) is False

    def test_host_qualified_allowlist_entry_matches_url(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, '[teatree]\nprivate_repos = ["gitlab.com/customercorp-engineering/their-svc"]\n')
        payload = "Context: https://gitlab.com/customercorp-engineering/their-svc/-/merge_requests/42"
        assert term_only_inside_own_repo_urls(payload, "customercorp", config_path=cfg) is True

    def test_no_allowlist_entry_never_downgrades(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, "[teatree]\n")
        payload = "Context: https://gitlab.com/customercorp-engineering/their-svc/-/issues/8223"
        assert term_only_inside_own_repo_urls(payload, "customercorp", config_path=cfg) is False

    def test_term_in_own_url_and_a_foreign_url_still_blocks(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, '[teatree]\nprivate_repos = ["customercorp-engineering"]\n')
        payload = (
            "Own: https://gitlab.com/customercorp-engineering/their-svc/-/issues/8223 "
            "and unrelated https://example.com/customercorp-blog/post-1"
        )
        assert term_only_inside_own_repo_urls(payload, "customercorp", config_path=cfg) is False

    def test_term_not_present_returns_false(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, '[teatree]\nprivate_repos = ["customercorp-engineering"]\n')
        payload = "Nothing to see here, just a public note."
        assert term_only_inside_own_repo_urls(payload, "customercorp", config_path=cfg) is False

    def test_github_own_repo_url_downgrades(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, '[teatree]\nprivate_repos = ["customercorp-engineering"]\n')
        payload = "Upstream fix: https://github.com/customercorp-engineering/their-svc/pull/17"
        assert term_only_inside_own_repo_urls(payload, "customercorp", config_path=cfg) is True

    def test_empty_payload_or_term_returns_false(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, '[teatree]\nprivate_repos = ["customercorp-engineering"]\n')
        assert term_only_inside_own_repo_urls("", "customercorp", config_path=cfg) is False
        assert term_only_inside_own_repo_urls("https://gitlab.com/x/y", "", config_path=cfg) is False
