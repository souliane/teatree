from teatree.utils.unified_diff import added_lines


def test_added_lines_carry_their_new_file_numbers_across_hunks() -> None:
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,3 @@\n keep\n-gone\n+new one\n+new two\n@@ -20,1 +21,2 @@\n ctx\n+late\n"

    assert added_lines(diff) == {2: "new one", 3: "new two", 22: "late"}


def test_lines_before_the_first_hunk_are_not_added_lines() -> None:
    assert added_lines("+orphan\n") == {}
