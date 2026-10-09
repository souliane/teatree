import inspect

from teatree.cli.setup.mandated_skills import MandatedSkillProvisioner


def test_mandated_provisioner_exposes_no_exclusion_input() -> None:
    assert "excluded" not in inspect.signature(MandatedSkillProvisioner).parameters
