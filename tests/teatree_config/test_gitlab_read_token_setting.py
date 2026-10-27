import pytest

from teatree.config.declaration import Category
from teatree.config.registries import COLD_SETTINGS
from teatree.config.secret_settings import is_credential_reference


def test_gitlab_read_token_pass_paths_is_private_mapping() -> None:
    setting = COLD_SETTINGS["gitlab_read_token_pass_paths"]

    assert setting.default == {}
    assert setting.category is Category.SECRET
    assert setting.parse({"group/repo": "pass-entry"}) == {"group/repo": "pass-entry"}
    assert is_credential_reference("gitlab_read_token_pass_paths")
    with pytest.raises(TypeError):
        setting.parse(["pass-entry"])
