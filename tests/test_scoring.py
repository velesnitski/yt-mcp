import time
from yt_mcp.scoring import (
    compute_active_score,
    compute_blocked_score,
    format_score_breakdown,
    _get_priority_name,
    _get_type_name,
    _count_blockers,
    _days_since_update,
)


def _make_issue(
    priority="Normal",
    issue_type="Task",
    state="In Progress",
    tags=None,
    updated_days_ago=0,
    blocker_count=0,
):
    """Build a minimal issue dict for scoring tests."""
    now_ms = int(time.time() * 1000)
    updated_ms = now_ms - (updated_days_ago * 86400 * 1000)

    links = []
    if blocker_count > 0:
        links.append({
            "direction": "OUTWARD",
            "linkType": {"name": "Subtask"},
            "issues": [{"idReadable": f"X-{i}"} for i in range(blocker_count)],
        })

    return {
        "idReadable": "TEST-1",
        "summary": "Test issue",
        "updated": updated_ms,
        "created": updated_ms - 86400000,
        "state": {"name": state},
        "priority": {"name": priority},
        "assignee": {"name": "Tester"},
        "tags": [{"name": t} for t in (tags or [])],
        "customFields": [
            {"name": "Priority", "value": {"name": priority}},
            {"name": "Type", "value": {"name": issue_type}},
        ],
        "links": links,
    }


# --- Helper tests ---

class TestGetPriorityName:
    def test_top_level(self):
        issue = {"priority": {"name": "Critical"}, "customFields": []}
        assert _get_priority_name(issue) == "Critical"

    def test_custom_field_fallback(self):
        issue = {
            "customFields": [{"name": "Priority", "value": {"name": "High"}}]
        }
        assert _get_priority_name(issue) == "High"

    def test_missing_priority(self):
        issue = {"customFields": []}
        assert _get_priority_name(issue) == ""


class TestGetTypeName:
    def test_from_custom_field(self):
        issue = {"customFields": [{"name": "Type", "value": {"name": "Bug"}}]}
        assert _get_type_name(issue) == "Bug"

    def test_missing_type(self):
        issue = {"customFields": []}
        assert _get_type_name(issue) == ""


class TestCountBlockers:
    def test_no_links(self):
        assert _count_blockers({"links": []}) == 0

    def test_outward_subtasks(self):
        issue = {
            "links": [{
                "direction": "OUTWARD",
                "linkType": {"name": "Subtask"},
                "issues": [{"idReadable": "A-1"}, {"idReadable": "A-2"}],
            }]
        }
        assert _count_blockers(issue) == 2

    def test_inward_links_not_counted(self):
        issue = {
            "links": [{
                "direction": "INWARD",
                "linkType": {"name": "Subtask"},
                "issues": [{"idReadable": "A-1"}],
            }]
        }
        assert _count_blockers(issue) == 0

    def test_depends_link(self):
        issue = {
            "links": [{
                "direction": "OUTWARD",
                "linkType": {"name": "Depend"},
                "issues": [{"idReadable": "A-1"}],
            }]
        }
        assert _count_blockers(issue) == 1

    def test_relates_not_counted(self):
        issue = {
            "links": [{
                "direction": "OUTWARD",
                "linkType": {"name": "Relates"},
                "issues": [{"idReadable": "A-1"}],
            }]
        }
        assert _count_blockers(issue) == 0

    def test_no_links_key(self):
        assert _count_blockers({}) == 0


class TestDaysSinceUpdate:
    def test_recently_updated(self):
        now_ms = int(time.time() * 1000)
        assert _days_since_update({"updated": now_ms}) == 0

    def test_old_update(self):
        old_ms = int(time.time() * 1000) - (10 * 86400 * 1000)
        assert _days_since_update({"updated": old_ms}) >= 9

    def test_no_updated_field(self):
        assert _days_since_update({}) == 0


# --- Active scoring ---

class TestComputeActiveScore:
    def test_basic_score(self):
        issue = _make_issue(priority="Normal", issue_type="Task", state="In Progress")
        score, breakdown = compute_active_score(issue)
        assert breakdown["priority"] == 15
        assert breakdown["type"] == 5
        assert breakdown["state"] == 10
        assert score == sum(breakdown.values())

    def test_critical_bug_in_progress(self):
        issue = _make_issue(priority="Critical", issue_type="Bug", state="In Progress")
        score, breakdown = compute_active_score(issue)
        assert breakdown["priority"] == 80
        assert breakdown["type"] == 20
        assert breakdown["state"] == 10
        assert score >= 110

    def test_show_stopper_with_tags(self):
        issue = _make_issue(
            priority="Show-stopper", issue_type="Bug",
            state="In Progress", tags=["Critical", "Urgent"],
        )
        score, breakdown = compute_active_score(issue)
        assert breakdown["priority"] == 100
        assert breakdown["tags"] == 70  # 40 + 30

    def test_staleness_bonus(self):
        issue = _make_issue(updated_days_ago=15)
        _, breakdown = compute_active_score(issue)
        assert breakdown["staleness"] == 15

        issue = _make_issue(updated_days_ago=8)
        _, breakdown = compute_active_score(issue)
        assert breakdown["staleness"] == 10

        issue = _make_issue(updated_days_ago=4)
        _, breakdown = compute_active_score(issue)
        assert breakdown["staleness"] == 5

        issue = _make_issue(updated_days_ago=1)
        _, breakdown = compute_active_score(issue)
        assert breakdown["staleness"] == 0

    def test_blocker_bonus(self):
        issue = _make_issue(blocker_count=2)
        _, breakdown = compute_active_score(issue)
        assert breakdown["blockers"] == 50  # 2 * 25

    def test_blocker_cap(self):
        issue = _make_issue(blocker_count=10)
        _, breakdown = compute_active_score(issue)
        assert breakdown["blockers"] == 100  # capped

    def test_missing_everything(self):
        issue = {"customFields": [], "tags": [], "links": []}
        score, breakdown = compute_active_score(issue)
        assert score == 0
        assert all(v == 0 for v in breakdown.values())

    def test_submitted_state_no_bonus(self):
        issue = _make_issue(state="Submitted")
        _, breakdown = compute_active_score(issue)
        assert breakdown["state"] == 0


# --- Blocked scoring ---

class TestComputeBlockedScore:
    def test_basic_blocked(self):
        issue = _make_issue(priority="High", issue_type="Feature", state="Blocked")
        score, breakdown = compute_blocked_score(issue)
        assert breakdown["priority"] == 60
        assert breakdown["type"] == 10
        assert "state" not in breakdown  # blocked model has no state bonus

    def test_duration_frozen(self):
        issue = _make_issue(updated_days_ago=100)
        _, breakdown = compute_blocked_score(issue)
        assert breakdown["duration"] == 30

    def test_duration_long(self):
        issue = _make_issue(updated_days_ago=35)
        _, breakdown = compute_blocked_score(issue)
        assert breakdown["duration"] == 20

    def test_duration_stale(self):
        issue = _make_issue(updated_days_ago=16)
        _, breakdown = compute_blocked_score(issue)
        assert breakdown["duration"] == 15

    def test_duration_aging(self):
        issue = _make_issue(updated_days_ago=9)
        _, breakdown = compute_blocked_score(issue)
        assert breakdown["duration"] == 10

    def test_duration_recent(self):
        issue = _make_issue(updated_days_ago=2)
        _, breakdown = compute_blocked_score(issue)
        assert breakdown["duration"] == 0

    def test_blocked_with_blockers(self):
        issue = _make_issue(priority="Critical", blocker_count=3)
        _, breakdown = compute_blocked_score(issue)
        assert breakdown["blockers"] == 75  # 3 * 25


# --- Format ---

class TestFormatScoreBreakdown:
    def test_only_nonzero(self):
        result = format_score_breakdown({"priority": 80, "type": 0, "tags": 40})
        assert "priority=80" in result
        assert "tags=40" in result
        assert "type" not in result

    def test_all_zero(self):
        result = format_score_breakdown({"priority": 0, "type": 0})
        assert result == ""
