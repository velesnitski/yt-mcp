"""Tests for risk detection false-positive fixes."""
import time

from yt_mcp.tools.monitoring import (
    BLOCKED_RISK_DAYS,
    NEW_ISSUE_GRACE_HOURS,
    WORKING_STATES,
    WAITING_STATES,
    _compute_health_score,
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
