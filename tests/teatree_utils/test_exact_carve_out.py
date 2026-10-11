"""``mask_exempt`` blanks exactly the occurrences an allow entry names, and nothing that extends one."""

import pytest

from teatree.utils.exact_carve_out import mask_exempt

HANDLE = "acme-owner"
ALIAS = "alice.work"
ADDRESS = "alice@acme.example"


def test_an_exact_occurrence_is_blanked_in_place() -> None:
    text = f"ask {HANDLE} today"

    assert mask_exempt(text, (HANDLE,)) == "ask " + " " * len(HANDLE) + " today"


def test_an_empty_allowlist_leaves_the_text_alone() -> None:
    assert mask_exempt(f"ask {HANDLE}", ()) == f"ask {HANDLE}"


def test_an_entry_without_tokens_names_nothing() -> None:
    assert mask_exempt("a --- b", ("---",)) == "a --- b"


@pytest.mark.parametrize("joined", ["acme.owner", "acme_owner", "acme owner", "AcmeOwner", "ACME-OWNER"])
def test_joining_separators_and_case_are_one_identifier(joined: str) -> None:
    assert mask_exempt(joined, (HANDLE,)).strip() == ""


@pytest.mark.parametrize("separated", ["acme/owner", "acme - owner", "acme@owner"])
def test_any_other_separator_is_a_different_identifier(separated: str) -> None:
    assert mask_exempt(separated, (HANDLE,)) == separated


def test_an_address_needs_its_own_at_sign() -> None:
    assert mask_exempt(ADDRESS.replace("@", "-"), (ADDRESS,)) == ADDRESS.replace("@", "-")


@pytest.mark.parametrize("extended", [f"x{HANDLE}", f"{HANDLE}y", f"é{HANDLE}", f"Pre{HANDLE.title()}"])
def test_a_letter_glued_to_either_end_extends_the_identifier(extended: str) -> None:
    assert mask_exempt(extended, (HANDLE,)) == extended


@pytest.mark.parametrize("extended", [f"x+{ADDRESS}", f"{ADDRESS}+x", f"{ADDRESS}@x"])
def test_an_address_mark_joined_to_a_word_extends_an_address(extended: str) -> None:
    assert mask_exempt(extended, (ADDRESS,)) == extended


@pytest.mark.parametrize(
    ("marker", "entry", "tail"), [("+", ALIAS, " ping"), ("+", ADDRESS, ""), ("@", ALIAS, " ping")]
)
def test_a_leading_diff_marker_does_not_extend_an_address_entry(marker: str, entry: str, tail: str) -> None:
    assert mask_exempt(f"{marker}{entry}{tail}", (ALIAS, ADDRESS)) == marker + " " * len(entry) + tail


def test_a_dotted_neighbour_does_not_extend_an_undotted_entry() -> None:
    assert mask_exempt(f"dev@{HANDLE}.example", (HANDLE,)).split() == ["dev@", ".example"]


def test_only_the_exact_occurrence_among_several_is_blanked() -> None:
    masked = mask_exempt(f"{HANDLE} and {HANDLE}-bot", (HANDLE,))

    assert masked.split() == ["and", f"{HANDLE}-bot"]


def test_the_longer_of_two_overlapping_entries_is_blanked_whole() -> None:
    longer = f"{HANDLE}-team"

    assert mask_exempt(longer, (HANDLE, longer)).strip() == ""
