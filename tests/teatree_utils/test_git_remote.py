from teatree.utils import git_remote
from teatree.utils.git_remote import host_from_remote


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


class TestHostFromRemote:
    """The host half of a remote URL — the identity bit ``slug_from_remote`` drops.

    ``owner/repo`` is unique per FORGE, not globally, so any caller deciding "is this
    clone the one the issue lives on" needs both halves.
    """

    def test_ssh_scp_form(self) -> None:
        assert host_from_remote("git@github.com:acme/widgets.git") == "github.com"

    def test_ssh_url_form_with_port(self) -> None:
        assert git_remote.host_from_remote("ssh://git@gitlab.com:22/acme/widgets.git") == "gitlab.com"

    def test_https_form_is_case_insensitive(self) -> None:
        assert git_remote.host_from_remote("https://GitHub.COM/acme/widgets") == "github.com"

    def test_https_form_with_port(self) -> None:
        assert git_remote.host_from_remote("https://git.example.org:8443/acme/widgets.git") == "git.example.org"

    def test_www_prefix_is_not_a_different_host(self) -> None:
        assert git_remote.host_from_remote("https://www.github.com/acme/widgets") == "github.com"

    def test_git_protocol_form(self) -> None:
        assert git_remote.host_from_remote("git://github.com/acme/widgets.git") == "github.com"

    def test_file_url_names_no_host(self) -> None:
        assert git_remote.host_from_remote("file:///srv/mirrors/widgets.git") == ""

    def test_filesystem_path_names_no_host(self) -> None:
        assert git_remote.host_from_remote("/srv/mirrors/widgets.git") == ""
        assert git_remote.host_from_remote("../sibling/widgets.git") == ""

    def test_empty_returns_empty(self) -> None:
        assert git_remote.host_from_remote("") == ""

    def test_an_ssh_alias_token_is_returned_verbatim_for_the_caller_to_resolve(self) -> None:
        # `github-work` is a ~/.ssh/config Host alias. This module is pure, so it
        # reports the token; expanding it needs `ssh -G`, which the caller owns.
        assert git_remote.host_from_remote("git@github-work:acme/widgets.git") == "github-work"
