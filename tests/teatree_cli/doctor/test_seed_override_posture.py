"""Doctor reports deliberate loop suppression without a false warning."""

from unittest.mock import patch

from teatree.cli.doctor.checks_loop import _check_shipped_seed_inertness
from teatree.loops.seed_inertness import KIND_DISABLED_VS_SHIPPED, KIND_SUPPRESSED, InertFinding


def test_manual_override_is_info_with_preset_remedy(capsys) -> None:
    finding = InertFinding(
        family="loop",
        name="inbox",
        kind=KIND_DISABLED_VS_SHIPPED,
        detail="deliberately forced OFF; use the active preset/mode mask to keep a loop off",
        is_fault=False,
    )
    with patch("teatree.loops.seed_inertness.shipped_inertness", return_value=(finding,)):
        assert _check_shipped_seed_inertness()
    output = capsys.readouterr().out
    assert "INFO  Shipped loop override" in output
    assert "preset/mode mask" in output
    assert "WARN" not in output


def test_preset_mask_is_healthy_and_quiet(capsys) -> None:
    finding = InertFinding(family="loop", name="dream", kind=KIND_SUPPRESSED, detail="masked by mode", is_fault=False)
    with patch("teatree.loops.seed_inertness.shipped_inertness", return_value=(finding,)):
        assert _check_shipped_seed_inertness()
    assert capsys.readouterr().out == ""
