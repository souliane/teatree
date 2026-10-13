"""``mask_exempt`` blanks exactly the occurrences an allow entry names, and nothing that extends one."""

import pytest

from teatree.utils.exact_carve_out import mask_exempt

HANDLE = "acme-owner"
ALIAS = "alice.work"
ADDRESS = "alice@acme.example"
ARABIC_INDIC_TWO = chr(0x662)


@pytest.mark.parametrize("joined", ["acme.owner", "acme_owner"])
def test_joining_separators_are_one_identifier(joined: str) -> None:
    assert mask_exempt(joined, (HANDLE,)).strip() == ""


@pytest.mark.parametrize("separated", ["acme/owner", "acme - owner"])
def test_any_other_separator_is_a_different_identifier(separated: str) -> None:
    assert mask_exempt(separated, (HANDLE,)) == separated


def test_an_address_needs_its_own_at_sign() -> None:
    assert mask_exempt(ADDRESS.replace("@", "-"), (ADDRESS,)) == ADDRESS.replace("@", "-")


@pytest.mark.parametrize(
    "extended",
    [
        f"x{HANDLE}",
        f"{HANDLE}y",
        f"é{HANDLE}",
        "2AcmeOwner",
        f"{HANDLE}{ARABIC_INDIC_TWO}",
        f"_{HANDLE}",
    ],
)
def test_a_letter_digit_or_joiner_glued_to_either_end_extends_the_identifier(extended: str) -> None:
    assert mask_exempt(extended, (HANDLE,)) == extended


@pytest.mark.parametrize(
    ("entry", "extended"),
    [
        (ADDRESS, f"x+{ADDRESS}"),
        (ADDRESS, f"{ADDRESS}+x"),
        (ADDRESS, f"{ADDRESS}@x"),
        ("alice@acme", "bob.alice@acme"),
        ("@acme-owner", "acme-owner.evil"),
        (ALIAS, f"bob@{ALIAS}"),
        (ALIAS, f"ping {ARABIC_INDIC_TWO}.{ALIAS}"),
        (ALIAS, f"{ALIAS}.{ARABIC_INDIC_TWO}"),
    ],
)
def test_an_address_mark_joined_to_a_word_extends_an_address(entry: str, extended: str) -> None:
    assert mask_exempt(extended, (entry,)) == extended


def test_only_the_exact_occurrences_among_several_are_blanked() -> None:
    masked = mask_exempt(f"{HANDLE}-bot, {HANDLE} and {HANDLE}", (HANDLE,))

    assert masked.split() == [f"{HANDLE}-bot,", "and"]


def test_a_mark_after_a_space_does_not_extend_an_address_entry() -> None:
    assert mask_exempt(f"ping @{ALIAS}", (ALIAS,)) == "ping @" + " " * len(ALIAS)
