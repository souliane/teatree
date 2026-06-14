from teatree.utils import git_remote


class TestSlugFromRemote:
    def test_github_ssh(self) -> None:
        assert git_remote.slug_from_remote("git@github.com:acme/widgets.git") == "acme/widgets"

    def test_github_https(self) -> None:
        assert git_remote.slug_from_remote("https://github.com/acme/widgets.git") == "acme/widgets"

    def test_gitlab_nested_namespace(self) -> None:
        assert git_remote.slug_from_remote("git@gitlab.com:acme/team/backend.git") == "acme/team/backend"

    def test_no_dot_git_suffix(self) -> None:
        assert git_remote.slug_from_remote("https://github.com/acme/widgets") == "acme/widgets"

    def test_empty_returns_empty(self) -> None:
        assert git_remote.slug_from_remote("") == ""


class TestWebBaseFromRemote:
    def test_ssh_form(self) -> None:
        assert git_remote.web_base_from_remote("git@github.com:acme/widgets.git") == "https://github.com"

    def test_ssh_url_form(self) -> None:
        assert git_remote.web_base_from_remote("ssh://git@gitlab.com/acme/widgets.git") == "https://gitlab.com"

    def test_https_form(self) -> None:
        assert git_remote.web_base_from_remote("https://gitlab.com/acme/widgets") == "https://gitlab.com"

    def test_self_hosted_host_preserved(self) -> None:
        assert git_remote.web_base_from_remote("git@git.example.org:acme/widgets.git") == "https://git.example.org"

    def test_empty_returns_empty(self) -> None:
        assert git_remote.web_base_from_remote("") == ""

    def test_unparsable_host_returns_empty(self) -> None:
        assert git_remote.web_base_from_remote("not-a-remote-url") == ""
