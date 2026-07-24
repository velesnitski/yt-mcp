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
    build_lookback_clause,
    _round_robin_balance,
    _is_team_pool,
    _is_blocked_by_unresolved,
    _filter_issues,
    _COLUMN_PATTERNS_match_any,
    _issue_to_dict,
    _render_markdown,
    _is_active,
    _is_too_overdue,
    _filter_active,
    _filter_not_too_overdue,
    _aggregate_payloads,
    _render_multi_markdown,
    _classify_board_columns,
    _build_pipeline_lane_states,
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
        return {"closed": kw.get("closed", 14), "released": kw.get("released", 0),
                "incoming": kw.get("incoming", 14), "reopened": kw.get("reopened", 0)}

    def _make_pipeline(self, **kw):
        # Default pipeline reflects a healthy 14-closed-per-30d team:
        # weekly_velocity ≈ 3.3, WIP cap ≈ 6, pipeline_total cap = 28.
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
        # incoming=30, closed=14 → 2.1× rate (>1.3 threshold)
        flags = compute_insights(self._make_metrics(incoming=30, closed=14), self._make_pipeline(), [], NOW_MS)
        assert any("backlog growing" in f.lower() for f in flags)

    def test_quality_concern(self):
        # 4 reopens / 14 closed ≈ 28% (>20% threshold)
        flags = compute_insights(self._make_metrics(reopened=4, closed=14), self._make_pipeline(), [], NOW_MS)
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

    def test_wip_overload_concurrent_dev_work(self):
        """True WIP overload: devs juggling too many concurrent items.
        weekly_velocity ≈ 1.2, WIP cap = 2.4 — in_progress=10 should fire."""
        pipeline = self._make_pipeline(in_progress=10, for_review=0, ready_for_test=0, on_testing=0)
        flags = compute_insights(self._make_metrics(closed=5), pipeline, [], NOW_MS)
        assert any("wip overload" in f.lower() and "in progress" in f.lower() for f in flags)

    def test_pipeline_overload_downstream_clog(self):
        """Pipeline overload (downstream-of-dev): heavy test queue.
        Same closed=14 baseline, but ready_for_test bloats pipeline_total."""
        pipeline = self._make_pipeline(in_progress=3, for_review=3, ready_for_test=25, on_testing=3)
        flags = compute_insights(self._make_metrics(closed=14), pipeline, [], NOW_MS)
        assert any("pipeline overload" in f.lower() for f in flags)

    def test_downstream_clog_shape_does_not_false_flag_wip(self):
        """Real-world shape: 5 in_progress + 6 for_review + 34 ready_for_test
        at ~28 closed/30d. The old "WIP overload" label was misleading because
        the math summed downstream-of-dev queues. With the split, WIP (which
        only counts in_progress) must NOT fire when devs are at 5 with a
        weekly_velocity of 6.5 (cap 13). The downstream clog should be
        surfaced by the bottleneck flag instead, not a misnamed WIP flag."""
        pipeline = self._make_pipeline(
            in_progress=5, for_review=6, ready_for_test=34, on_testing=0,
        )
        flags = compute_insights(self._make_metrics(closed=28, incoming=28), pipeline, [], NOW_MS)
        wip_flags = [f for f in flags if "wip overload" in f.lower()]
        assert wip_flags == [], f"unexpected WIP overload flag: {wip_flags}"
        # Bottleneck flag should still surface the test-queue congestion
        assert any("bottleneck" in f.lower() for f in flags), (
            f"expected bottleneck flag to catch the test queue: {flags}"
        )

    def test_both_flags_fire_when_both_conditions_met(self):
        # High in_progress AND high pipeline_total
        pipeline = self._make_pipeline(in_progress=10, for_review=5, ready_for_test=20, on_testing=5)
        flags = compute_insights(self._make_metrics(closed=10), pipeline, [], NOW_MS)
        assert any("wip overload" in f.lower() for f in flags)
        assert any("pipeline overload" in f.lower() for f in flags)

    def test_lookback_days_affects_velocity_calc(self):
        """Same closed count over a shorter window → higher weekly velocity →
        higher WIP cap. The same in_progress count should NOT fire on a
        14d window what it would fire on a 30d window."""
        pipeline = self._make_pipeline(in_progress=4)
        # 30d window: closed=10 → 2.3/wk → cap 4.6 → in_progress=4 is OK
        flags_30 = compute_insights(self._make_metrics(closed=10), pipeline, [], NOW_MS, lookback_days=30)
        # 14d window: closed=10 → 5/wk → cap 10 → also fine
        flags_14 = compute_insights(self._make_metrics(closed=10), pipeline, [], NOW_MS, lookback_days=14)
        # Now flip — in_progress high enough to fire on 30d but not on 14d
        pipeline_high = self._make_pipeline(in_progress=8)
        flags_high_30 = compute_insights(self._make_metrics(closed=10), pipeline_high, [], NOW_MS, lookback_days=30)
        flags_high_14 = compute_insights(self._make_metrics(closed=10), pipeline_high, [], NOW_MS, lookback_days=14)
        # 30d: in_progress=8 > cap 4.6 → fires
        assert any("wip overload" in f.lower() for f in flags_high_30)
        # 14d: in_progress=8 < cap 10 → no fire
        assert not any("wip overload" in f.lower() for f in flags_high_14)

    def test_team_underloaded(self):
        # in_flight=1 vs closed=15 → underloaded (1 < 15/3=5)
        pipeline = self._make_pipeline(in_progress=1, for_review=0)
        flags = compute_insights(self._make_metrics(closed=15), pipeline, [], NOW_MS)
        assert any("underloaded" in f.lower() for f in flags)


# --- Date-clause format (fixes the YouTrack 400 parse error) ----------------

import re as _re_for_tests


class TestBuildLookbackClause:
    """The query parser rejects `-Nd .. *` — must be absolute ISO dates."""

    def test_returns_absolute_iso_range(self):
        clause = build_lookback_clause(30, NOW_MS)
        # Match `YYYY-MM-DD .. YYYY-MM-DD`
        assert _re_for_tests.fullmatch(
            r"\d{4}-\d{2}-\d{2} \.\. \d{4}-\d{2}-\d{2}", clause
        ), f"unexpected clause: {clause!r}"

    def test_no_relative_offset_in_output(self):
        clause = build_lookback_clause(30, NOW_MS)
        # The old broken format used these — make sure they never appear.
        assert "-30d" not in clause
        assert " * " not in clause
        assert clause.count("..") == 1


class TestResolvedDateAttribute:
    """Pulse must query `resolved date:`, never the `resolved:` alias.

    A 2026-07 YouTrack Cloud upgrade broke the alias for ranges: spaced
    ranges 400, and UNspaced ranges parse but silently match zero issues
    (ADR-035). Pin the source so a refactor can't reintroduce either form.
    """

    def test_no_bare_resolved_alias_in_pulse_queries(self):
        import inspect
        import yt_mcp.tools.pulse as pulse_mod
        src = inspect.getsource(pulse_mod)
        # Every query interpolation must use `resolved date:`; a bare
        # `resolved: {` f-string interpolation is the broken alias.
        assert "resolved: {" not in src.replace("resolved date:", ""), (
            "pulse queries must use `resolved date:` — the bare `resolved:` "
            "alias 400s (spaced) or silently matches nothing (unspaced)"
        )

    def test_30_day_window_spans_30_days(self):
        clause = build_lookback_clause(30, NOW_MS)
        start, end = clause.split(" .. ")
        start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        assert (end_dt - start_dt).days == 30

    def test_14_day_window(self):
        clause = build_lookback_clause(14, NOW_MS)
        start, end = clause.split(" .. ")
        start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        assert (end_dt - start_dt).days == 14

    def test_end_date_matches_now(self):
        clause = build_lookback_clause(30, NOW_MS)
        _, end = clause.split(" .. ")
        expected_end = datetime.fromtimestamp(NOW_MS / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        assert end == expected_end


# --- JSON payload shape ----------------------------------------------------

class TestIssueToDict:
    def test_basic_shape(self):
        issue = _issue(id="PROJ-1", summary="Hello", cf_severity="Major", cf_type="Bug")
        d = _issue_to_dict(issue, score=4.2, breakdown={"severity": 3}, now_ms=NOW_MS)
        assert d["id"] == "PROJ-1"
        assert d["summary"] == "Hello"
        assert d["severity"] == "Major"
        assert d["type"] == "Bug"
        assert d["score"] == 4.2
        assert d["breakdown"] == {"severity": 3}

    def test_deadline_days_computed(self):
        issue = _issue(cf_deadline="2026-05-22")  # 4 days from NOW
        d = _issue_to_dict(issue, now_ms=NOW_MS)
        assert d["deadline_days"] == 4

    def test_assignee_login_from_top_level_field(self):
        issue = {
            "idReadable": "PROJ-1", "summary": "x", "customFields": [],
            "assignee": {"login": "alice.a", "name": "Alice A"},
        }
        d = _issue_to_dict(issue, now_ms=NOW_MS)
        assert d["assignee"] == "Alice A"
        assert d["assignee_login"] == "alice.a"

    def test_assignee_login_from_custom_field(self):
        # Some YT projects put Assignee in customFields rather than top-level
        issue = _issue(id="PROJ-1", cf_assignee="Bob B")
        d = _issue_to_dict(issue, now_ms=NOW_MS)
        assert d["assignee"] == "Bob B"
        assert d["assignee_login"] == "bob.b"

    def test_assignee_login_none_when_unassigned(self):
        d = _issue_to_dict(_issue(id="PROJ-1"), now_ms=NOW_MS)
        assert d["assignee"] == "Unassigned"
        assert d["assignee_login"] is None

    def test_assignee_login_none_when_only_name_present(self):
        # Sometimes the API returns name without login (display-only assignee)
        issue = {
            "idReadable": "PROJ-1", "summary": "x", "customFields": [],
            "assignee": {"name": "Charlie C"},  # no login
        }
        d = _issue_to_dict(issue, now_ms=NOW_MS)
        assert d["assignee"] == "Charlie C"
        assert d["assignee_login"] is None

    def test_no_deadline_yields_none(self):
        d = _issue_to_dict(_issue(), now_ms=NOW_MS)
        assert d["deadline_days"] is None

    def test_age_days_in_state(self):
        issue = _issue(updated=NOW_MS - 7 * DAY_MS)
        d = _issue_to_dict(issue, now_ms=NOW_MS)
        assert d["age_days_in_state"] == 7


class TestRenderMarkdownFromPayload:
    """The renderer must accept a payload dict (no raw issues)."""

    def _make_payload(self, **kw) -> dict:
        return {
            "board": kw.get("board", "Foo Board"),
            "lookback_days": kw.get("lookback_days", 30),
            "horizon_days": kw.get("horizon_days", 14),
            "metrics": kw.get("metrics", {
                "closed": 5, "released": 0, "incoming": 5, "reopened": 0,
            }),
            "pipeline_counts": kw.get("pipeline_counts", {
                "in_progress": 3, "for_review": 2, "ready_for_test": 1,
                "on_testing": 1, "ready_for_release": 0,
            }),
            "has_released_states": kw.get("has_released_states", False),
            "triaged": kw.get("triaged", []),
            "re_entry": kw.get("re_entry", []),
            "incoming": kw.get("incoming", []),
            "team_balanced": kw.get("team_balanced", []),
            "team_pool": kw.get("team_pool", []),
            "insights": kw.get("insights", []),
            "unmapped_columns": kw.get("unmapped_columns", []),
            "unknown_columns": kw.get("unknown_columns", []),
        }

    def test_header_contains_board_and_windows(self):
        out = _render_markdown(self._make_payload(), limit=10)
        assert "Foo Board" in out
        assert "←30d" in out and "14d→" in out

    def test_empty_queue_message(self):
        out = _render_markdown(self._make_payload(), limit=10)
        assert "Nothing in the forward queue" in out

    def test_insights_rendered(self):
        out = _render_markdown(
            self._make_payload(insights=["📈 Backlog growing — 10 new vs 5 closed"]),
            limit=10,
        )
        assert "Flags" in out
        assert "Backlog growing" in out

    def test_unmapped_columns_diagnostic_via_new_key(self):
        # v1.12.3 renamed the payload key to `unmapped_columns`. The
        # renderer prefers the new key when both are present.
        out = _render_markdown(
            self._make_payload(unmapped_columns=["Approval Pending"]),
            limit=10,
        )
        assert "Approval Pending" in out
        assert "skipped" in out.lower()
        assert "not State values" in out

    def test_unmapped_columns_diagnostic_via_legacy_key(self):
        # Old payload key still works (backwards-compat for callers that
        # serialized payloads from v1.11.x).
        out = _render_markdown(
            self._make_payload(unknown_columns=["Approval Pending"]),
            limit=10,
        )
        assert "Approval Pending" in out

    def test_triaged_items_shown(self):
        item = _issue_to_dict(
            _issue(id="PROJ-7", summary="Build widget", cf_severity="Major"),
            score=3.0, now_ms=NOW_MS,
        )
        out = _render_markdown(self._make_payload(triaged=[item]), limit=10)
        assert "PROJ-7" in out
        assert "Ready to pull" in out

    def test_team_balanced_section(self):
        item = _issue_to_dict(_issue(id="PROJ-9"), score=2.0, now_ms=NOW_MS)
        balanced = [{"assignee": "Alice A", "items": [item]}]
        out = _render_markdown(self._make_payload(team_balanced=balanced), limit=10)
        assert "Team-balanced" in out
        assert "Alice A" in out
        assert "PROJ-9" in out

    def test_pool_section_when_present(self):
        item = _issue_to_dict(_issue(id="PROJ-11"), score=1.5, now_ms=NOW_MS)
        out = _render_markdown(
            self._make_payload(team_pool=[item], team_balanced=[]), limit=10,
        )
        assert "Available to claim" in out
        assert "PROJ-11" in out

    def test_shipped_line_appears_only_when_release_state_present(self):
        no_release = _render_markdown(self._make_payload(has_released_states=False), limit=10)
        assert "Shipped:" not in no_release
        with_release = _render_markdown(
            self._make_payload(has_released_states=True,
                               metrics={"closed": 5, "released": 2, "incoming": 5, "reopened": 0}),
            limit=10,
        )
        assert "Shipped:" in with_release


# --- format param round-trip (JSON parseable) ------------------------------

class TestFormatJsonOutput:
    """The format='json' branch should return a parseable JSON string with
    the expected top-level keys — consumers can json.loads() and template."""

    def test_payload_round_trip(self):
        import json as _json
        # Mirror what the tool builds, then dump+load to verify shape.
        payload = {
            "board": "Foo Board",
            "lookback_days": 30,
            "horizon_days": 14,
            "metrics": {"closed": 5, "released": 2, "incoming": 5, "reopened": 0},
            "pipeline_counts": {"in_progress": 3, "for_review": 2,
                                "ready_for_test": 1, "on_testing": 1,
                                "ready_for_release": 0},
            "has_released_states": True,
            "triaged": [_issue_to_dict(_issue(id="PROJ-1"), score=3.0, now_ms=NOW_MS)],
            "re_entry": [],
            "incoming": [],
            "team_balanced": [],
            "team_pool": [],
            "insights": ["📈 Backlog growing"],
            "unknown_columns": [],
        }
        s = _json.dumps(payload, indent=2, ensure_ascii=False)
        parsed = _json.loads(s)
        assert parsed["board"] == "Foo Board"
        assert parsed["metrics"]["closed"] == 5
        assert parsed["triaged"][0]["id"] == "PROJ-1"
        assert parsed["insights"] == ["📈 Backlog growing"]
        # Non-ASCII (emoji) preserved
        assert "📈" in s


# --- Stale (max_idle_days) and overdue (max_overdue_days) filters ----------

class TestIsActive:
    def test_recent_update_is_active(self):
        i = _issue(updated=NOW_MS - 5 * DAY_MS)
        assert _is_active(i, 60, NOW_MS) is True

    def test_old_update_is_inactive(self):
        i = _issue(updated=NOW_MS - 90 * DAY_MS)
        assert _is_active(i, 60, NOW_MS) is False

    def test_at_exact_threshold_is_active(self):
        # 60 days exactly — boundary kept (≤ check)
        i = _issue(updated=NOW_MS - 60 * DAY_MS)
        assert _is_active(i, 60, NOW_MS) is True

    def test_one_day_past_threshold_is_inactive(self):
        i = _issue(updated=NOW_MS - 61 * DAY_MS)
        assert _is_active(i, 60, NOW_MS) is False

    def test_zero_disables_filter(self):
        ancient = _issue(updated=NOW_MS - 365 * DAY_MS)
        assert _is_active(ancient, 0, NOW_MS) is True

    def test_missing_updated_is_kept(self):
        # Surface ambiguous data rather than silently dropping
        i = {"idReadable": "PROJ-1", "summary": "x", "customFields": []}
        assert _is_active(i, 60, NOW_MS) is True

    def test_falls_back_to_created_when_updated_absent(self):
        i = {"idReadable": "PROJ-1", "summary": "x", "customFields": [],
             "created": NOW_MS - 90 * DAY_MS}
        assert _is_active(i, 60, NOW_MS) is False


class TestIsTooOverdue:
    def test_no_deadline_is_not_overdue(self):
        assert _is_too_overdue(_issue(), 30, NOW_MS) is False

    def test_future_deadline_is_not_overdue(self):
        i = _issue(cf_deadline="2026-06-18")  # ~31d in the future
        assert _is_too_overdue(i, 30, NOW_MS) is False

    def test_recently_overdue_is_kept(self):
        # 5d past deadline, threshold 30 → keep
        i = _issue(cf_deadline="2026-05-13")
        assert _is_too_overdue(i, 30, NOW_MS) is False

    def test_deeply_overdue_dropped(self):
        # 60d past deadline, threshold 30 → drop
        i = _issue(cf_deadline="2026-03-19")
        assert _is_too_overdue(i, 30, NOW_MS) is True

    def test_zero_disables_filter(self):
        i = _issue(cf_deadline="2025-01-01")  # 500+ days past
        assert _is_too_overdue(i, 0, NOW_MS) is False


class TestFilterActiveBatch:
    def test_filters_inactive_out(self):
        recent = _issue(id="PROJ-1", updated=NOW_MS - 5 * DAY_MS)
        ancient = _issue(id="PROJ-2", updated=NOW_MS - 200 * DAY_MS)
        kept = _filter_active([recent, ancient], 60, NOW_MS)
        ids = [i["idReadable"] for i in kept]
        assert ids == ["PROJ-1"]

    def test_zero_keeps_everything(self):
        recent = _issue(id="PROJ-1", updated=NOW_MS - 5 * DAY_MS)
        ancient = _issue(id="PROJ-2", updated=NOW_MS - 200 * DAY_MS)
        kept = _filter_active([recent, ancient], 0, NOW_MS)
        assert len(kept) == 2


class TestFilterNotTooOverdueBatch:
    def test_filters_deeply_overdue_out(self):
        ok = _issue(id="PROJ-1")  # no deadline → kept
        recently_late = _issue(id="PROJ-2", cf_deadline="2026-05-15")  # 3d late → kept
        zombie = _issue(id="PROJ-3", cf_deadline="2026-02-01")          # 100+d late → dropped
        kept = _filter_not_too_overdue([ok, recently_late, zombie], 30, NOW_MS)
        ids = {i["idReadable"] for i in kept}
        assert ids == {"PROJ-1", "PROJ-2"}

    def test_zero_keeps_everything(self):
        zombie = _issue(id="PROJ-1", cf_deadline="2025-01-01")
        assert _filter_not_too_overdue([zombie], 0, NOW_MS) == [zombie]


class TestFilterSemanticBoundaries:
    """Confirm filters don't accidentally clobber the keep-on-uncertain rule."""

    def test_no_updated_or_created_is_kept(self):
        weird = {"idReadable": "PROJ-1", "summary": "no timestamps", "customFields": []}
        assert _is_active(weird, 60, NOW_MS) is True

    def test_no_deadline_not_dropped_by_overdue_filter(self):
        no_dl = _issue(id="PROJ-1")
        assert _filter_not_too_overdue([no_dl], 30, NOW_MS) == [no_dl]


# --- Multi-team pulse: aggregation + multi-board rendering -----------------

def _make_payload(**kw) -> dict:
    """Mock a payload from _build_pulse_payload for aggregation tests."""
    return {
        "board": kw.get("board", "Foo Board"),
        "lookback_days": kw.get("lookback_days", 30),
        "horizon_days": kw.get("horizon_days", 14),
        "metrics": kw.get("metrics", {
            "closed": 10, "released": 2, "incoming": 8, "reopened": 1,
        }),
        "pipeline_counts": kw.get("pipeline_counts", {
            "in_progress": 3, "for_review": 2, "ready_for_test": 1,
            "on_testing": 1, "ready_for_release": 0,
        }),
        "has_released_states": kw.get("has_released_states", False),
        "triaged": kw.get("triaged", []),
        "re_entry": kw.get("re_entry", []),
        "incoming": kw.get("incoming", []),
        "team_balanced": kw.get("team_balanced", []),
        "team_pool": kw.get("team_pool", []),
        "insights": kw.get("insights", []),
        "unmapped_columns": kw.get("unmapped_columns", []),
        "unknown_columns": kw.get("unknown_columns", []),
    }


class TestAggregatePayloads:
    def test_sums_metrics_across_boards(self):
        p1 = _make_payload(metrics={"closed": 5, "released": 1, "incoming": 4, "reopened": 0})
        p2 = _make_payload(metrics={"closed": 7, "released": 2, "incoming": 6, "reopened": 1})
        agg = _aggregate_payloads([p1, p2], lookback_days=30, horizon_days=14)
        assert agg["metrics"] == {"closed": 12, "released": 3, "incoming": 10, "reopened": 1}

    def test_sums_pipeline_counts_across_boards(self):
        p1 = _make_payload(pipeline_counts={
            "in_progress": 2, "for_review": 1, "ready_for_test": 0,
            "on_testing": 0, "ready_for_release": 0,
        })
        p2 = _make_payload(pipeline_counts={
            "in_progress": 3, "for_review": 4, "ready_for_test": 5,
            "on_testing": 1, "ready_for_release": 2,
        })
        agg = _aggregate_payloads([p1, p2], lookback_days=30, horizon_days=14)
        assert agg["pipeline_counts"]["in_progress"] == 5
        assert agg["pipeline_counts"]["for_review"] == 5
        assert agg["pipeline_counts"]["ready_for_test"] == 5
        assert agg["pipeline_counts"]["on_testing"] == 1
        assert agg["pipeline_counts"]["ready_for_release"] == 2

    def test_board_count_reflects_input_size(self):
        agg = _aggregate_payloads([_make_payload() for _ in range(7)], lookback_days=30, horizon_days=14)
        assert agg["board_count"] == 7

    def test_flag_counts(self):
        p1 = _make_payload(insights=["a", "b"])
        p2 = _make_payload(insights=[])
        p3 = _make_payload(insights=["c"])
        agg = _aggregate_payloads([p1, p2, p3], lookback_days=30, horizon_days=14)
        assert agg["total_flags"] == 3
        assert agg["boards_with_flags"] == 2

    def test_has_any_released_state_true_when_any(self):
        p1 = _make_payload(has_released_states=False)
        p2 = _make_payload(has_released_states=True)
        agg = _aggregate_payloads([p1, p2], lookback_days=30, horizon_days=14)
        assert agg["has_any_released_state"] is True

    def test_has_any_released_state_false_when_none(self):
        p = _make_payload(has_released_states=False)
        agg = _aggregate_payloads([p], lookback_days=30, horizon_days=14)
        assert agg["has_any_released_state"] is False

    def test_empty_input(self):
        agg = _aggregate_payloads([], lookback_days=30, horizon_days=14)
        assert agg["board_count"] == 0
        assert agg["metrics"]["closed"] == 0


class TestRenderMultiMarkdown:
    def test_header_shows_board_count_and_windows(self):
        aggregate = _aggregate_payloads(
            [_make_payload(board="A"), _make_payload(board="B")],
            lookback_days=30, horizon_days=14,
        )
        out = _render_multi_markdown(
            aggregate,
            [_make_payload(board="A"), _make_payload(board="B")],
            limit=5,
        )
        assert "Org pulse" in out
        assert "2 boards" in out
        assert "←30d" in out and "14d→" in out

    def test_per_board_sections_present(self):
        payloads = [
            _make_payload(board="Alpha"),
            _make_payload(board="Beta"),
        ]
        aggregate = _aggregate_payloads(payloads, lookback_days=30, horizon_days=14)
        out = _render_multi_markdown(aggregate, payloads, limit=5)
        assert "## Alpha" in out
        assert "## Beta" in out

    def test_shipped_line_only_when_any_board_has_release_state(self):
        no_release = [_make_payload(has_released_states=False, board="A")]
        agg_no = _aggregate_payloads(no_release, lookback_days=30, horizon_days=14)
        out_no = _render_multi_markdown(agg_no, no_release, limit=5)
        assert "Shipped:" not in out_no

        with_release = [
            _make_payload(has_released_states=False, board="A"),
            _make_payload(has_released_states=True, board="B",
                          metrics={"closed": 5, "released": 3, "incoming": 4, "reopened": 0}),
        ]
        agg_yes = _aggregate_payloads(with_release, lookback_days=30, horizon_days=14)
        out_yes = _render_multi_markdown(agg_yes, with_release, limit=5)
        assert "Shipped:" in out_yes

    def test_per_board_flags_rendered_under_summary(self):
        p = _make_payload(board="Alpha", insights=["📈 Backlog growing — 10 vs 5"])
        agg = _aggregate_payloads([p], lookback_days=30, horizon_days=14)
        out = _render_multi_markdown(agg, [p], limit=5)
        assert "Backlog growing" in out

    def test_per_board_top_items_truncated_to_limit(self):
        triaged_items = [
            _issue_to_dict(_issue(id=f"PROJ-{i}", summary=f"Item {i}"),
                           score=10.0 - i, now_ms=NOW_MS)
            for i in range(10)
        ]
        p = _make_payload(board="Alpha", triaged=triaged_items)
        agg = _aggregate_payloads([p], lookback_days=30, horizon_days=14)
        out = _render_multi_markdown(agg, [p], limit=2)
        # limit=2 caps per-section display; in multi-view we additionally cap
        # at 3 internally. We should see top 2 items rendered.
        assert "PROJ-0" in out
        assert "PROJ-1" in out


class TestClassifyBoardColumns:
    def test_extracts_state_to_role(self):
        board = {
            "columnSettings": {
                "columns": [
                    {"presentation": "To Do", "fieldValues": [{"name": "To Do"}]},
                    {"presentation": "In Progress", "fieldValues": [{"name": "In Progress"}]},
                    {"presentation": "Done", "fieldValues": [{"name": "Closed"}]},
                ]
            }
        }
        state_to_role, unknown = _classify_board_columns(board)
        assert state_to_role["To Do"] == "triaged"
        assert state_to_role["In Progress"] == "in_progress"
        assert state_to_role["Closed"] == "done"
        assert unknown == []

    def test_unmapped_column_when_no_field_values_skipped_from_query_set(self):
        # v1.12.3: presentation-only columns are UI labels (swimlanes/
        # groupings), not real State enum values. Querying
        # `State: {Approval Pending}` against a project where that's not a
        # state triggers 400 — so we surface the column for diagnostics
        # but never query it.
        board = {
            "columnSettings": {
                "columns": [
                    {"presentation": "Approval Pending", "fieldValues": []},
                ]
            }
        }
        state_to_role, unmapped = _classify_board_columns(board)
        # NOT in the queryable state set
        assert "Approval Pending" not in state_to_role
        # IS in the diagnostic list
        assert "Approval Pending" in unmapped

    def test_mix_of_real_states_and_presentation_only_columns(self):
        board = {
            "columnSettings": {
                "columns": [
                    {"presentation": "To Do", "fieldValues": [{"name": "To Do"}]},
                    {"presentation": "Swimlane Label", "fieldValues": []},
                    {"presentation": "In Progress", "fieldValues": [{"name": "In Progress"}]},
                ]
            }
        }
        state_to_role, unmapped = _classify_board_columns(board)
        assert state_to_role == {"To Do": "triaged", "In Progress": "in_progress"}
        assert unmapped == ["Swimlane Label"]


class TestBuildPipelineLaneStates:
    def test_groups_into_lanes(self):
        state_to_role = {
            "In Progress": "in_progress",
            "For review": "in_progress",
            "Ready for test": "in_progress",
            "On testing": "in_progress",
            "Ready for release": "in_progress",
            "To Do": "triaged",   # not in_progress, ignored
        }
        lanes = _build_pipeline_lane_states(state_to_role)
        assert "In Progress" in lanes["in_progress"]
        assert "For review" in lanes["for_review"]
        assert "Ready for test" in lanes["ready_for_test"]
        assert "On testing" in lanes["on_testing"]
        assert "Ready for release" in lanes["ready_for_release"]
        # Triaged state shouldn't show up in any lane
        for v in lanes.values():
            assert "To Do" not in v
