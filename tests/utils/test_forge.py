from teatree.utils.forge import forge_from_remote


class TestForgeFromRemote:
    def test_github_ssh(self) -> None:
        assert forge_from_remote("git@github.com:acme/widgets.git") == "github"

    def test_github_https_issue_url(self) -> None:
        assert forge_from_remote("https://github.com/acme/widgets/issues/1") == "github"

    def test_gitlab_dotcom(self) -> None:
        assert forge_from_remote("git@gitlab.com:acme/widgets.git") == "gitlab"

    def test_self_hosted_gitlab(self) -> None:
        assert forge_from_remote("git@gitlab.example.com:acme/widgets.git") == "gitlab"

    def test_unrecognised_host_is_empty(self) -> None:
        assert forge_from_remote("git@git.example.org:acme/widgets.git") == ""

    def test_empty_is_empty(self) -> None:
        assert forge_from_remote("") == ""
