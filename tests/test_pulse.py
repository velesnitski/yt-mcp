"""Tests for the Team Pulse tool.

Coverage:
- column-role classifier (every state name from real workflows)
- ranking formula (severity > priority, deadline bonus, stale bonus)
- filters (standup/report regex, blocked-by-unresolved)
- team-balanced round-robin distribution
- insight flags (backlog growth, quality concern, bottleneck, deadline cliff, stale triaged)
- pool-bucket isolation (synthetic team-pool assignees)
"""

from datetime import datetime, timezone

import pytest

from yt_mcp.tools.pulse import (
    classify_column,
    compute_pulse_score,
    compute_insights,
    _round_robin_balance,
    _is_team_pool,
    _is_blocked_by_unresolved,
    _filter_issues,
    _COLUMN_PATTERNS_match_any,
)
from yt_mcp.tools.deadlines.parser import (
    _DEFAULT_STANDUP_PATTERNS, _compile_standup_patterns,
)


NOW_MS = int(datetime(2026, 5, 18, tzinfo=timezone.utc).timestamp() * 1000)
DAY_MS = 86400 * 1000


def _issue(**kw) -> dict:
    """Build a minimal issue dict from kwargs."""
    cfs = []
    for fname in ("Severity", "Type", "Priority", "Assignee", "Deadline ☠️"):
        val = kw.pop(f"cf_{fname.split()[0].lower()}", None)
        if val is not None:
            if fname == "Deadline ☠️":
                cfs.append({"name": fname, "value": {"presentation": val}})
            elif fname == "Assignee":
                cfs.append({"name": fname, "value": {"login": val.lower().replace(" ", "."), "name": val}})
            else:
                cfs.append({"name": fname, "value": {"name": val}})
    state = kw.pop("state", "")
    issue = {
        "idReadable": kw.pop("id", "PROJ-1"),
        "summary": kw.pop("summary", "Some task"),
        "updated": kw.pop("updated", NOW_MS),
        "created": kw.pop("created", NOW_MS),
        "customFields": cfs,
    }
    if state:
        issue["state"] = {"name": state}
    if "links" in kw:
        issue["links"] = kw.pop("links")
    return issue


# --- Column classifier ----------------------------------------------------

class TestClassifyColumn:
    """Real-world state names from active workflows."""

    @pytest.mark.parametrize("state,expected", [
        # Triaged (ready to pull)
        ("To Do", "triaged"),
        ("TODO", "triaged"),
        ("Backlog", "triaged"),
        ("Ready for Dev", "triaged"),
        ("Selected for Dev", "triaged"),
        # Incoming (raw)
        ("Submitted", "incoming"),
        ("New", "incoming"),
        ("Open", "incoming"),
        ("Reported", "incoming"),
        # Re-entry
        ("For revision", "re_entry"),
        ("ReOpen", "re_entry"),
        ("Reopen", "re_entry"),
        ("Rejected", "re_entry"),
        ("Needs Rework", "re_entry"),
        ("На доработку", "re_entry"),
        # Paused
        ("Blocked", "paused"),
        ("Pause", "paused"),
        ("On hold", "paused"),
        ("Waiting", "paused"),
        # In-progress lane
        ("In Progress", "in_progress"),
        ("For review", "in_progress"),
        ("In Review", "in_progress"),
        ("Ready for test", "in_progress"),
        ("On testing", "in_progress"),
        ("Ready for release", "in_progress"),
        # Done
        ("Closed", "done"),
        ("Done", "done"),
        ("Resolved", "done"),
        ("Released", "done"),
        ("Verified", "done"),
        ("Fixed", "done"),
    ])
    def test_known_states(self, state, expected):
        assert classify_column(state) == expected

    def test_empty_defaults_to_triaged(self):
        assert classify_column("") == "triaged"

    def test_unknown_defaults_to_triaged(self):
        assert classify_column("Approval pending") == "triaged"

    def test_match_any_detects_known(self):
        assert _COLUMN_PATTERNS_match_any("To Do")
        assert _COLUMN_PATTERNS_match_any("Submitted")
        assert not _COLUMN_PATTERNS_match_any("Approval pending")


# --- Ranking --------------------------------------------------------------

class TestComputePulseScore:
    def test_severity_dominates(self):
        blocker = _issue(cf_severity="Blocker")
        trivial = _issue(cf_severity="Trivial")
        b_score, _ = compute_pulse_score(blocker, NOW_MS)
        t_score, _ = compute_pulse_score(trivial, NOW_MS)
        assert b_score > t_score
        # Severity alone gives Blocker=5, Trivial=0 → 5-point gap baseline
        assert b_score - t_score >= 5

    def test_bug_type_bonus(self):
        bug = _issue(cf_severity="Major", cf_type="Bug")
        tech = _issue(cf_severity="Major", cf_type="Tech task")
        doc = _issue(cf_severity="Major", cf_type="Documentation")
        assert compute_pulse_score(bug, NOW_MS)[0] > compute_pulse_score(tech, NOW_MS)[0]
        assert compute_pulse_score(tech, NOW_MS)[0] > compute_pulse_score(doc, NOW_MS)[0]

    def test_deadline_proximity(self):
        soon = _issue(cf_deadline="2026-05-22")  # 4 days from NOW (2026-05-18)
        far = _issue(cf_deadline="2026-08-18")   # 92 days
        assert compute_pulse_score(soon, NOW_MS)[0] > compute_pulse_score(far, NOW_MS)[0]
        # ≤7d gets +4 bonus
        breakdown = compute_pulse_score(soon, NOW_MS)[1]
        assert breakdown["deadline"] == 4.0

    def test_overdue_deadline_does_not_exceed_imminent(self):
        overdue = _issue(cf_deadline="2026-05-15")  # 3 days overdue
        imminent = _issue(cf_deadline="2026-05-22")  # 4 days away
        assert compute_pulse_score(overdue, NOW_MS)[1]["deadline"] == 4.0
        assert compute_pulse_score(imminent, NOW_MS)[1]["deadline"] == 4.0

    def test_stale_in_state_capped(self):
        # 120 days old; raw bonus would be 6.0, cap at 3.0
        stale = _issue(updated=NOW_MS - 120 * DAY_MS)
        breakdown = compute_pulse_score(stale, NOW_MS)[1]
        assert breakdown["stale"] == 3.0

    def test_priority_tiebreak(self):
        # All else equal, High beats Low
        high = _issue(cf_severity="Major", cf_priority="High")
        low = _issue(cf_severity="Major", cf_priority="Low")
        assert compute_pulse_score(high, NOW_MS)[0] > compute_pulse_score(low, NOW_MS)[0]

    def test_breakdown_keys(self):
        _, breakdown = compute_pulse_score(_issue(cf_severity="Critical"), NOW_MS)
        assert set(breakdown.keys()) == {"severity", "type", "deadline", "stale", "priority"}


# --- Filters --------------------------------------------------------------

class TestStandupFilter:
    def test_team_report_caught(self):
        patterns = _compile_standup_patterns({})
        from yt_mcp.tools.deadlines.parser import _is_standup
        # Generic "<Group> Team. Report DD.MM.YYYY" — must be filtered
        assert _is_standup("Foo Team. Report 13.04.2026", patterns)
        assert _is_standup("Bar Team Report 11.03.2026", patterns)
        # Daily / standup remain caught
        assert _is_standup("Engineering Daily", patterns)
        assert _is_standup("Sprint Standup", patterns)

    def test_real_work_not_filtered(self):
        patterns = _compile_standup_patterns({})
        from yt_mcp.tools.deadlines.parser import _is_standup
        assert not _is_standup("Implement new login flow", patterns)
        assert not _is_standup("Reporting dashboard rewrite", patterns)  # "report" alone doesn't trigger


class TestBlockedByUnresolved:
    def test_blocked_by_open_blocker(self):
        issue = _issue(links=[{
            "direction": "outward",
            "linkType": {"name": "Depend"},
            "issues": [{"idReadable": "PROJ-99", "state": {"name": "Open"}}],
        }])
        assert _is_blocked_by_unresolved(issue)

    def test_not_blocked_when_blocker_closed(self):
        issue = _issue(links=[{
            "direction": "outward",
            "linkType": {"name": "Depend"},
            "issues": [{"idReadable": "PROJ-99", "state": {"name": "Closed"}}],
        }])
        assert not _is_blocked_by_unresolved(issue)

    def test_no_links_not_blocked(self):
        assert not _is_blocked_by_unresolved(_issue())

    def test_inward_link_ignored(self):
        # If something depends on us, we're not blocked by it
        issue = _issue(links=[{
            "direction": "inward",
            "linkType": {"name": "Depend"},
            "issues": [{"idReadable": "PROJ-99", "state": {"name": "Open"}}],
        }])
        assert not _is_blocked_by_unresolved(issue)


class TestFilterIssues:
    def test_drops_standup_and_blocked(self):
        patterns = _compile_standup_patterns({})
        good = _issue(id="PROJ-1", summary="Real work")
        standup = _issue(id="PROJ-2", summary="Foo Team. Report 13.04.2026")
        blocked = _issue(id="PROJ-3", summary="Real but blocked", links=[{
            "direction": "outward", "linkType": {"name": "Depend"},
            "issues": [{"state": {"name": "Open"}}],
        }])
        kept = _filter_issues([good, standup, blocked], patterns)
        ids = [i["idReadable"] for i in kept]
        assert ids == ["PROJ-1"]


# --- Team-pool detection --------------------------------------------------

class TestTeamPoolDetection:
    @pytest.mark.parametrize("name,expected", [
        ("Backend Team", True),
        ("Frontend team", True),
        ("Команда разработки", True),  # Russian "team"
        ("Foo Bar Team", True),
        ("John Doe", False),
        ("Maria Garcia", False),
        ("", False),
    ])
    def test_classification(self, name, expected):
        assert _is_team_pool(name) is expected


# --- Round-robin balancing -----------------------------------------------

class TestRoundRobinBalance:
    def test_round_robin_distributes_evenly(self):
        # 3 items for Alice, 1 for Bob
        items = [
            (_issue(id="PROJ-1", cf_assignee="Alice A"), 5.0),
            (_issue(id="PROJ-2", cf_assignee="Alice A"), 4.0),
            (_issue(id="PROJ-3", cf_assignee="Alice A"), 3.0),
            (_issue(id="PROJ-4", cf_assignee="Bob B"), 4.5),
        ]
        result = _round_robin_balance(items)
        assert "Alice A" in result
        assert "Bob B" in result
        assert len(result["Alice A"]) == 3
        assert len(result["Bob B"]) == 1
        # Alice's items should be in score-desc order within her queue
        scores = [s for _i, s in result["Alice A"]]
        assert scores == sorted(scores, reverse=True)

    def test_team_pool_separated(self):
        items = [
            (_issue(id="PROJ-1", cf_assignee="Alice A"), 5.0),
            (_issue(id="PROJ-2", cf_assignee="Foo Team"), 4.0),
        ]
        result = _round_robin_balance(items)
        assert "Alice A" in result
        assert "Foo Team" not in result
        assert "__pool__" in result
        assert len(result["__pool__"]) == 1

    def test_unassigned_goes_to_pool(self):
        items = [(_issue(id="PROJ-1"), 5.0)]  # no assignee
        result = _round_robin_balance(items)
        assert "__pool__" in result


# --- Insights -------------------------------------------------------------

class TestComputeInsights:
    def _make_metrics(self, **kw):
        return {"closed": kw.get("closed", 5), "released": kw.get("released", 0),
                "incoming": kw.get("incoming", 5), "reopened": kw.get("reopened", 0)}

    def _make_pipeline(self, **kw):
        return {
            "in_progress": kw.get("in_progress", 3),
            "for_review": kw.get("for_review", 2),
            "ready_for_test": kw.get("ready_for_test", 1),
            "on_testing": kw.get("on_testing", 1),
            "ready_for_release": kw.get("ready_for_release", 0),
        }

    def test_healthy_state_no_flags(self):
        flags = compute_insights(self._make_metrics(), self._make_pipeline(), [], NOW_MS)
        assert flags == []

    def test_zero_throughput_flagged(self):
        flags = compute_insights(self._make_metrics(closed=0), self._make_pipeline(), [], NOW_MS)
        assert any("velocity unknown" in f.lower() or "no work closed" in f.lower() for f in flags)

    def test_backlog_growing(self):
        # incoming=10, closed=5 → 2× rate
        flags = compute_insights(self._make_metrics(incoming=10, closed=5), self._make_pipeline(), [], NOW_MS)
        assert any("backlog growing" in f.lower() for f in flags)

    def test_quality_concern(self):
        # 3 reopens / 5 closed = 60%
        flags = compute_insights(self._make_metrics(reopened=3, closed=5), self._make_pipeline(), [], NOW_MS)
        assert any("quality" in f.lower() for f in flags)

    def test_bottleneck_detected(self):
        # for_review has 6, ready_for_test has 1 → bottleneck at review
        pipeline = self._make_pipeline(in_progress=2, for_review=6, ready_for_test=1, on_testing=1)
        flags = compute_insights(self._make_metrics(), pipeline, [], NOW_MS)
        assert any("bottleneck" in f.lower() and "review" in f.lower() for f in flags)

    def test_deadline_cliff(self):
        triaged = [_issue(cf_deadline="2026-05-22") for _ in range(3)]  # 4d away each
        flags = compute_insights(self._make_metrics(), self._make_pipeline(), triaged, NOW_MS)
        assert any("deadline cliff" in f.lower() for f in flags)

    def test_stale_triaged(self):
        triaged = [_issue(updated=NOW_MS - 60 * DAY_MS) for _ in range(3)]
        flags = compute_insights(self._make_metrics(), self._make_pipeline(), triaged, NOW_MS)
        assert any("stale triaged" in f.lower() for f in flags)

    def test_wip_overload(self):
        # pipeline_total = 4+4+3+3+0 = 14 vs closed=5 (2.8×) → overload
        pipeline = self._make_pipeline(in_progress=4, for_review=4, ready_for_test=3, on_testing=3)
        flags = compute_insights(self._make_metrics(closed=5), pipeline, [], NOW_MS)
        assert any("wip overload" in f.lower() for f in flags)

    def test_team_underloaded(self):
        # in_flight=1 vs closed=15 → underloaded (1 < 15/3=5)
        pipeline = self._make_pipeline(in_progress=1, for_review=0)
        flags = compute_insights(self._make_metrics(closed=15), pipeline, [], NOW_MS)
        assert any("underloaded" in f.lower() for f in flags)
