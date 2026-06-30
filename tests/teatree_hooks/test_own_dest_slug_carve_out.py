"""Tests for the own-destination-slug carve-out (`teatree.hooks.own_dest_slug_carve_out`).

``term_is_destination_own_slug`` downgrades a banned-term match when the post's
OWN destination repo slug IS the tripped term — the #2597 false positive where a
status comment mentioning the overlay name, posted to the overlay's own private
tracker (named after the overlay), was hard-blocked. The carve-out keys off the
RESOLVED DESTINATION SLUG (config-free), so a tracker named after the overlay
downgrades while a post to a public repo whose slug does NOT carry the term
(``souliane/teatree``) still blocks (the #2597 acceptance criteria).

Synthetic overlay name ``democorp`` only — no real overlay/customer name.
"""

from teatree.hooks.own_dest_slug_carve_out import term_is_destination_own_slug


class TestTermIsDestinationOwnSlug:
    def test_comment_to_private_tracker_named_after_overlay_downgrades(self) -> None:
        cmd = 'gh issue comment 5 -R democorp-eng/tracker --body "status: democorp rollout on track"'
        assert term_is_destination_own_slug(cmd, "democorp", None) is True

    def test_same_term_to_public_teatree_still_blocks(self) -> None:
        # Acceptance criterion 2: the public teatree slug does NOT carry the
        # overlay term, so the same comment to a public destination still blocks.
        cmd = 'gh issue create -R souliane/teatree --title x --body "democorp leak"'
        assert term_is_destination_own_slug(cmd, "democorp", None) is False

    def test_org_prefix_token_of_multi_token_slug_matches(self) -> None:
        # The overlay name is the ORG-PREFIX token of the tracker's own path.
        cmd = 'glab mr note 7 -R democorp-engineering/internal-tracker --message "democorp status"'
        assert term_is_destination_own_slug(cmd, "democorp", None) is True

    def test_url_positional_destination_resolves_and_downgrades(self) -> None:
        cmd = 'gh issue comment https://github.com/democorp-eng/tracker/issues/5 --body "democorp note"'
        assert term_is_destination_own_slug(cmd, "democorp", None) is True

    def test_t3_review_post_to_own_named_project_downgrades(self) -> None:
        cmd = 't3 review post-comment democorp-eng/tracker 5 --body "democorp status"'
        assert term_is_destination_own_slug(cmd, "democorp", None) is True

    def test_chained_public_post_defeats_downgrade(self) -> None:
        # A private post named after the overlay chained to a PUBLIC post whose
        # slug does NOT carry the term must NOT downgrade — the public surface
        # stays blocked.
        cmd = (
            'gh issue comment 5 -R democorp-eng/tracker --body "democorp" '
            '&& gh pr create -R souliane/teatree --body "democorp"'
        )
        assert term_is_destination_own_slug(cmd, "democorp", None) is False

    def test_substitution_construct_defeats_downgrade(self) -> None:
        cmd = 'glab mr note 7 -R democorp-eng/tracker --message "$(gh pr create -R souliane/teatree --body democorp)"'
        assert term_is_destination_own_slug(cmd, "democorp", None) is False

    def test_raw_api_write_is_not_eligible(self) -> None:
        # A raw ``gh``/``glab api`` WRITE can carry its body to any endpoint, so
        # it is never downgraded by the structured-destination slug carve-out.
        cmd = "gh api --method POST repos/democorp-eng/tracker/issues/5/comments -f body=democorp"
        assert term_is_destination_own_slug(cmd, "democorp", None) is False

    def test_foreign_term_not_in_slug_does_not_downgrade(self) -> None:
        # A genuine foreign customer term that is NOT the destination's own slug
        # stays blocked even on a post to the private-named tracker.
        cmd = 'gh issue comment 5 -R democorp-eng/tracker --body "othercorp secret"'
        assert term_is_destination_own_slug(cmd, "othercorp", None) is False

    def test_superset_term_longer_than_slug_run_does_not_downgrade(self) -> None:
        # A SUPERSET term (a longer token-run than the slug's own) is not
        # contained in the slug and stays blocked.
        cmd = 'gh issue comment 5 -R democorp/tracker --body "democorp-services secret"'
        assert term_is_destination_own_slug(cmd, "democorp-services", None) is False

    def test_git_commit_is_not_eligible(self) -> None:
        # The commit path keeps its own landing-repo carve-out; this destination
        # carve-out is for structured posts only.
        assert term_is_destination_own_slug('git commit -m "democorp"', "democorp", None) is False

    def test_no_publish_segment_does_not_downgrade(self) -> None:
        assert term_is_destination_own_slug("echo democorp", "democorp", None) is False

    def test_empty_command_does_not_downgrade(self) -> None:
        assert term_is_destination_own_slug("", "democorp", None) is False

    def test_chained_read_only_api_stays_eligible(self) -> None:
        # A read-only ``gh api`` GET posts no body, so it does not defeat the
        # downgrade when the actual post targets the own-named private tracker.
        cmd = (
            "gh api repos/democorp-eng/tracker/issues "
            '&& gh issue comment 5 -R democorp-eng/tracker --body "democorp status"'
        )
        assert term_is_destination_own_slug(cmd, "democorp", None) is True

    def test_unrecognised_leader_chain_defeats_downgrade(self) -> None:
        # An unrecognised executable can shell out to a hidden public post with
        # no forge token in its argv, so it is never provably inert and defeats
        # the downgrade (fail-closed, mirroring the destination skip).
        cmd = 'gh issue comment 5 -R democorp-eng/tracker --body "democorp" && make release'
        assert term_is_destination_own_slug(cmd, "democorp", None) is False
