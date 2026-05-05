"""Tests for risk detection false-positive fixes."""
import time

from yt_mcp.tools.monitoring import (
    BLOCKED_RISK_DAYS,
    NEW_ISSUE_GRACE_HOURS,
    WORKING_STATES,
    WAITING_STATES,
    COMPLETION_STATES,
    _compute_health_score,
    _count_flagged_issues,
    _hours_since,
)


def _ms_hours_ago(hours: float) -> int:
    """Return a ms timestamp from N hours ago."""
    return int((time.time() - hours * 3600) * 1000)


class TestHoursSince:
    def test_recent_timestamp(self):
        ms = _ms_hours_ago(2)
        assert 1.9 < _hours_since(ms) < 2.1

    def test_none_returns_zero(self):
        assert _hours_since(None) == 0.0

    def test_zero_returns_zero(self):
        assert _hours_since(0) == 0.0


class TestHealthScoreDedup:
    def test_empty_risks_perfect_score(self):
        assert _compute_health_score(10, {}) == 100

    def test_zero_total_perfect_score(self):
        assert _compute_health_score(0, {"stalled": [{"idReadable": "A-1"}]}) == 100

    def test_one_issue_one_category(self):
        # A-1 is stalled (weight 3) → 3/10 * 100 = 30% deduction
        risks = {"stalled": [{"idReadable": "A-1"}]}
        assert _compute_health_score(10, risks) == 70

    def test_dedup_multi_category(self):
        """One issue in 3 categories deducts once at worst weight."""
        issue = {"idReadable": "A-1"}
        risks = {
            "ancient": [issue],
            "unassigned": [issue],
            "blocked": [issue],
        }
        # Without dedup: (2+1+1) = 4 deductions
        # With dedup: max(2,1,1) = 2 deductions → 80
        assert _compute_health_score(10, risks) == 80

    def test_dedup_picks_highest_weight(self):
        """stalled(3) + ancient(2) on same issue uses stalled's weight."""
        issue = {"idReadable": "A-1"}
        risks = {"stalled": [issue], "ancient": [issue]}
        # Without dedup: 3+2 = 5
        # With dedup: 3 → 70
        assert _compute_health_score(10, risks) == 70

    def test_different_issues_all_count(self):
        """Two distinct issues both deduct."""
        risks = {
            "stalled": [{"idReadable": "A-1"}],
            "ancient": [{"idReadable": "A-2"}],
        }
        # 3 + 2 = 5 deductions → 50
        assert _compute_health_score(10, risks) == 50

    def test_issue_without_id_skipped(self):
        """Issues without idReadable are ignored."""
        risks = {"stalled": [{"summary": "no id"}]}
        assert _compute_health_score(10, risks) == 100

    def test_fallback_to_id_field(self):
        """Issues with 'id' instead of 'idReadable' still counted."""
        risks = {"blocked": [{"id": "internal-123"}]}
        # blocked weight 1, total 10 → 10% deduction
        assert _compute_health_score(10, risks) == 90


class TestConstants:
    def test_blocked_grace_period(self):
        assert BLOCKED_RISK_DAYS == 14

    def test_new_issue_grace_hours(self):
        assert NEW_ISSUE_GRACE_HOURS == 4

    def test_pause_in_waiting_states(self):
        assert "pause" in WAITING_STATES

    def test_working_states_complete(self):
        assert "in progress" in WORKING_STATES
        assert "in review" in WORKING_STATES

    def test_waiting_states_complete(self):
        assert "submitted" in WAITING_STATES
        assert "to do" in WAITING_STATES
        assert "reopen" in WAITING_STATES
        assert "open" in WAITING_STATES

    def test_working_states_unchanged(self):
        # On Testing / For Review must NOT be here — they belong in Ancient
        assert "on testing" not in WORKING_STATES
        assert "for review" not in WORKING_STATES

    def test_completion_states_present(self):
        assert "ready for release" in COMPLETION_STATES
        assert "backlog" in COMPLETION_STATES
        assert "won't fix" in COMPLETION_STATES
        assert "wontfix" in COMPLETION_STATES
        assert "rejected" in COMPLETION_STATES

    def test_active_states_excludes_pause(self):
        # ACTIVE_STATES is used by get_top_active_issues + dashboards.
        # Pause = explicit deferral, must not surface as "top active".
        from yt_mcp.formatters import ACTIVE_STATES
        assert "pause" not in ACTIVE_STATES
        assert "in progress" in ACTIVE_STATES


class TestCompletionStateExclusion:
    """Logic mirror — verify state checks exclude COMPLETION_STATES."""

    def test_ready_for_release_not_ancient(self):
        # Same logic as get_project_health line:
        # if days_open > 200 and state != "pause" and state not in COMPLETION_STATES
        state = "ready for release"
        is_ancient = (
            300 > 200
            and state != "pause"
            and state not in COMPLETION_STATES
        )
        assert is_ancient is False

    def test_backlog_not_ancient(self):
        state = "backlog"
        is_ancient = (
            300 > 200
            and state != "pause"
            and state not in COMPLETION_STATES
        )
        assert is_ancient is False

    def test_wontfix_not_ancient(self):
        for state in ("won't fix", "wontfix"):
            is_ancient = (
                500 > 200
                and state != "pause"
                and state not in COMPLETION_STATES
            )
            assert is_ancient is False, f"state={state}"

    def test_for_review_old_is_ancient_not_stalled(self):
        # "for review" stays out of WORKING_STATES, so old items end up Ancient
        state = "for review"
        is_stalled = state in WORKING_STATES
        is_ancient = (
            300 > 200
            and state != "pause"
            and state not in COMPLETION_STATES
        )
        assert is_stalled is False
        assert is_ancient is True

    def test_on_testing_old_is_ancient_not_stalled(self):
        state = "on testing"
        is_stalled = state in WORKING_STATES
        is_ancient = (
            300 > 200
            and state != "pause"
            and state not in COMPLETION_STATES
        )
        assert is_stalled is False
        assert is_ancient is True

    def test_completion_state_not_forgotten(self):
        # Forgotten gate from get_at_risk_issues:
        # state in WAITING_STATES and not pause and not in COMPLETION_STATES
        state = "backlog"  # in COMPLETION_STATES (not WAITING_STATES, but check both)
        is_forgotten = (
            state in WAITING_STATES
            and state != "pause"
            and state not in COMPLETION_STATES
        )
        assert is_forgotten is False


class TestCountFlaggedIssues:
    def test_empty(self):
        assert _count_flagged_issues({}) == 0

    def test_count_flagged_dedupes(self):
        # Same issue in 3 categories — counted once
        issue = {"idReadable": "A-1"}
        risks = {
            "stalled": [issue],
            "ancient": [issue],
            "blocked": [issue],
        }
        assert _count_flagged_issues(risks) == 1

    def test_count_flagged_distinct(self):
        risks = {
            "stalled": [{"idReadable": "A-1"}, {"idReadable": "A-2"}],
            "ancient": [{"idReadable": "A-3"}],
            "blocked": [{"idReadable": "A-1"}],  # duplicate of A-1
        }
        assert _count_flagged_issues(risks) == 3

    def test_count_flagged_skips_no_id(self):
        risks = {"stalled": [{"summary": "no id"}, {"idReadable": "A-1"}]}
        assert _count_flagged_issues(risks) == 1

    def test_count_flagged_uses_id_fallback(self):
        risks = {"blocked": [{"id": "internal-99"}]}
        assert _count_flagged_issues(risks) == 1
