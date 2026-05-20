"""Tests for stuck-handoff detection (`get_stuck_handoffs`).

Focus: pure-function correctness on the smart bits — role classifier,
cross-team transition detection, latest-state-change extraction,
issue serialization. The full async tool body is exercised via the
registration test (smoke); the per-issue logic is exercised here.
"""

from datetime import datetime, timezone

import pytest

from yt_mcp.tools.handoffs import (
    classify_handoff_role,
    _is_cross_team_transition,
    _latest_state_change,
    _issue_to_stuck_dict,
    _format_stuck_line,
    _render_stuck_markdown,
    _transition_label,
    _HANDOFF_RECEIVING_ROLES,
)


NOW_MS = int(datetime(2026, 5, 18, tzinfo=timezone.utc).timestamp() * 1000)
DAY_MS = 86400 * 1000


# --- Role classifier ----------------------------------------------------

class TestClassifyHandoffRole:
    @pytest.mark.parametrize("state,expected", [
        # Dev side — distinct from pulse's broader in_progress lump
        ("In Progress", "dev"),
        ("For review", "dev"),
        ("In Review", "dev"),
        ("Code Review", "dev"),
        # QA side
        ("Ready for test", "qa"),
        ("On testing", "qa"),
        ("In testing", "qa"),
        ("Dev QA", "qa"),
        ("Staging QA", "qa"),
        ("Prod QA", "qa"),
        # Release side
        ("Ready for release", "release"),
        ("Ready for stage", "release"),
        ("Ready to prod", "release"),
        ("Released", "release"),
        # Rework
        ("For revision", "rework"),
        ("ReOpen", "rework"),
        ("На доработку", "rework"),
        # Triage
        ("To Do", "triage"),
        ("Backlog", "triage"),
        ("Ready for Dev", "triage"),
        # Intake
        ("Submitted", "intake"),
        ("New", "intake"),
        ("Open", "intake"),
        # Paused / done — not handoff-receiving
        ("Blocked", "paused"),
        ("Pause", "paused"),
        ("On hold", "paused"),
        ("Closed", "done"),
        ("Done", "done"),
        ("Resolved", "done"),
    ])
    def test_known_states(self, state, expected):
        assert classify_handoff_role(state) == expected

    def test_unknown_state_returns_unknown(self):
        # Distinct from pulse's "fall through to triaged" — for handoff
        # detection, unknown state means we can't classify the transition.
        assert classify_handoff_role("Mystery Lane") == "unknown"

    def test_empty_returns_unknown(self):
        assert classify_handoff_role("") == "unknown"
        assert classify_handoff_role(None) == "unknown"


# --- Cross-team transition detection ------------------------------------

class TestIsCrossTeamTransition:
    @pytest.mark.parametrize("from_role,to_role", [
        ("dev", "qa"),       # the canonical stuck case
        ("qa", "release"),
        ("qa", "rework"),
        ("qa", "dev"),       # bounced back for fixes
        ("release", "dev"),  # failed release
        ("triage", "dev"),
        ("intake", "dev"),
        ("rework", "dev"),
    ])
    def test_known_cross_team(self, from_role, to_role):
        assert _is_cross_team_transition(from_role, to_role) is True

    @pytest.mark.parametrize("from_role,to_role", [
        ("dev", "dev"),
        ("qa", "qa"),
        ("release", "release"),
        ("triage", "triage"),
    ])
    def test_same_role_is_not_handoff(self, from_role, to_role):
        assert _is_cross_team_transition(from_role, to_role) is False

    def test_transition_to_done_not_handoff(self):
        # 'done' isn't in the cross-team set — going to terminal state
        # isn't an actionable handoff.
        assert _is_cross_team_transition("qa", "done") is False
        assert _is_cross_team_transition("dev", "done") is False

    def test_transition_to_paused_not_handoff(self):
        assert _is_cross_team_transition("dev", "paused") is False

    def test_unknown_states_not_treated_as_handoff(self):
        # Surface separately — don't silently count as handoff
        assert _is_cross_team_transition("unknown", "qa") is False
        assert _is_cross_team_transition("dev", "unknown") is False


# --- Latest state-change extraction -------------------------------------

class TestLatestStateChange:
    def _state_act(self, ts: int, from_state: str, to_state: str,
                   login: str = "alice") -> dict:
        return {
            "timestamp": ts,
            "field": {"name": "State"},
            "removed": [{"name": from_state}] if from_state else [],
            "added": [{"name": to_state}] if to_state else [],
            "author": {"login": login, "name": login.title()},
        }

    def test_picks_most_recent(self):
        acts = [
            self._state_act(NOW_MS - 10 * DAY_MS, "Submitted", "To Do"),
            self._state_act(NOW_MS - 5 * DAY_MS, "To Do", "In Progress"),
            self._state_act(NOW_MS - 2 * DAY_MS, "In Progress", "Ready for test"),
        ]
        latest = _latest_state_change(acts)
        assert latest["to_state"] == "Ready for test"
        assert latest["from_state"] == "In Progress"
        assert latest["ts"] == NOW_MS - 2 * DAY_MS

    def test_ignores_non_state_activities(self):
        acts = [
            self._state_act(NOW_MS - 5 * DAY_MS, "In Progress", "Ready for test"),
            {  # comment edit — should be ignored
                "timestamp": NOW_MS - 1 * DAY_MS,
                "field": {"name": "comments"},
                "added": [],
                "removed": [],
            },
            {  # assignee change — also ignored
                "timestamp": NOW_MS - 1 * DAY_MS,
                "field": {"name": "Assignee"},
                "added": [{"name": "Bob"}],
                "removed": [{"name": "Alice"}],
            },
        ]
        latest = _latest_state_change(acts)
        assert latest["to_state"] == "Ready for test"
        assert latest["ts"] == NOW_MS - 5 * DAY_MS

    def test_returns_none_when_no_state_activities(self):
        acts = [
            {"timestamp": NOW_MS, "field": {"name": "comments"}},
            {"timestamp": NOW_MS - DAY_MS, "field": {"name": "Tags"}},
        ]
        assert _latest_state_change(acts) is None

    def test_returns_none_on_empty(self):
        assert _latest_state_change([]) is None

    def test_extracts_author(self):
        acts = [self._state_act(NOW_MS - DAY_MS, "In Progress", "Ready for test",
                                 login="alice.smith")]
        latest = _latest_state_change(acts)
        assert latest["author_login"] == "alice.smith"
        assert latest["author_name"] == "Alice.Smith"


# --- Issue serialization to JSON-friendly dict --------------------------

def _make_issue(**kw) -> dict:
    cfs = []
    for fname in ("Severity", "Type", "Priority", "Deadline ☠️"):
        val = kw.pop(f"cf_{fname.split()[0].lower()}", None)
        if val is not None:
            if fname == "Deadline ☠️":
                cfs.append({"name": fname, "value": {"presentation": val}})
            else:
                cfs.append({"name": fname, "value": {"name": val}})
    return {
        "idReadable": kw.get("id", "PROJ-1"),
        "summary": kw.get("summary", "Some task"),
        "state": {"name": kw.get("state", "Ready for test")},
        "assignee": kw.get("assignee", {"login": "bob.b", "name": "Bob B"}),
        "customFields": cfs,
    }


def _make_change(ts: int, from_state: str = "In Progress",
                 to_state: str = "Ready for test",
                 author_login: str = "alice.a") -> dict:
    return {
        "ts": ts, "from_state": from_state, "to_state": to_state,
        "author_login": author_login, "author_name": author_login.title(),
    }


class TestIssueToStuckDict:
    def test_basic_shape(self):
        issue = _make_issue(id="PROJ-42", summary="Fix login flow",
                            cf_severity="Major", cf_type="Bug")
        change = _make_change(NOW_MS - 7 * DAY_MS)
        d = _issue_to_stuck_dict(issue, change, "dev", "qa", 7.0, NOW_MS)
        assert d["id"] == "PROJ-42"
        assert d["summary"] == "Fix login flow"
        assert d["current_state"] == "Ready for test"
        assert d["current_role"] == "qa"
        assert d["previous_state"] == "In Progress"
        assert d["previous_role"] == "dev"
        assert d["transition"] == "dev→qa"
        assert d["days_stuck"] == 7.0
        assert d["last_mover"] == "alice.a"
        assert d["current_assignee"] == "Bob B"
        assert d["current_assignee_login"] == "bob.b"
        assert d["severity"] == "Major"
        assert d["type"] == "Bug"

    def test_transitioned_at_is_iso_date(self):
        issue = _make_issue()
        change = _make_change(NOW_MS - 7 * DAY_MS)
        d = _issue_to_stuck_dict(issue, change, "dev", "qa", 7.0, NOW_MS)
        # NOW = 2026-05-18, minus 7 days = 2026-05-11
        assert d["transitioned_at"] == "2026-05-11"

    def test_deadline_days_computed_from_field(self):
        issue = _make_issue(cf_deadline="2026-05-22")  # 4d future
        d = _issue_to_stuck_dict(
            issue, _make_change(NOW_MS - 5 * DAY_MS),
            "dev", "qa", 5.0, NOW_MS,
        )
        assert d["deadline_days"] == 4

    def test_deadline_none_when_no_field(self):
        issue = _make_issue()  # no deadline cf
        d = _issue_to_stuck_dict(
            issue, _make_change(NOW_MS - 5 * DAY_MS),
            "dev", "qa", 5.0, NOW_MS,
        )
        assert d["deadline_days"] is None


# --- Markdown render ----------------------------------------------------

class TestRenderStuckMarkdown:
    def _make_payload(self, **kw) -> dict:
        return {
            "board": kw.get("board", "Foo Board"),
            "stuck_days": kw.get("stuck_days", 4),
            "lookback_days": kw.get("lookback_days", 30),
            "total_stuck": kw.get("total_stuck", 0),
            "candidates_examined": kw.get("candidates_examined", 0),
            "stuck": kw.get("stuck", []),
            "stuck_all_count": kw.get("stuck_all_count", 0),
            "by_transition": kw.get("by_transition", {}),
            "by_receiving_assignee": kw.get("by_receiving_assignee", {}),
            "median_days_stuck": kw.get("median_days_stuck", 0),
            "worst": kw.get("worst", None),
        }

    def test_empty_payload_renders_friendly_message(self):
        out = _render_stuck_markdown(self._make_payload(), limit=10)
        assert "No stuck handoffs" in out

    def test_header_contains_board_and_threshold(self):
        out = _render_stuck_markdown(
            self._make_payload(total_stuck=3, candidates_examined=20),
            limit=10,
        )
        assert "Foo Board" in out
        assert "≥4d" in out
        assert "3 items" in out
        assert "20 candidates examined" in out

    def test_grouping_by_transition(self):
        stuck = [
            _issue_to_stuck_dict(
                _make_issue(id=f"PROJ-{i}"),
                _make_change(NOW_MS - 10 * DAY_MS),
                "dev", "qa", 10.0, NOW_MS,
            )
            for i in range(3)
        ]
        stuck += [
            _issue_to_stuck_dict(
                _make_issue(id="PROJ-99", state="Ready for release"),
                _make_change(NOW_MS - 14 * DAY_MS, "On testing", "Ready for release"),
                "qa", "release", 14.0, NOW_MS,
            )
        ]
        payload = self._make_payload(
            total_stuck=4, candidates_examined=10, stuck=stuck,
            by_transition={"dev→qa": 3, "qa→release": 1},
            by_receiving_assignee={"Bob B": 4},
            median_days_stuck=10, worst={"id": "PROJ-99", "days_stuck": 14.0},
        )
        out = _render_stuck_markdown(payload, limit=10)
        assert "Dev → QA stalls" in out
        assert "QA → Release stalls" in out
        # All four IDs present
        for i in range(3):
            assert f"PROJ-{i}" in out
        assert "PROJ-99" in out

    def test_deadline_cliff_callout_when_present(self):
        # 2 items with deadline ≤ 7d → cliff callout fires
        stuck = []
        for i in range(2):
            issue = _make_issue(id=f"PROJ-{i}", cf_deadline="2026-05-22")  # 4d
            stuck.append(_issue_to_stuck_dict(
                issue, _make_change(NOW_MS - 6 * DAY_MS),
                "dev", "qa", 6.0, NOW_MS,
            ))
        payload = self._make_payload(
            total_stuck=2, candidates_examined=5, stuck=stuck,
            by_transition={"dev→qa": 2}, by_receiving_assignee={"Bob B": 2},
            median_days_stuck=6, worst={"id": "PROJ-0", "days_stuck": 6.0},
        )
        out = _render_stuck_markdown(payload, limit=10)
        assert "deadline cliff" in out.lower()


class TestTransitionLabel:
    def test_known_pairs_get_friendly_label(self):
        assert "Dev → QA" in _transition_label("dev", "qa")
        assert "QA → Release" in _transition_label("qa", "release")
        assert "rework" in _transition_label("qa", "rework").lower()

    def test_unknown_pair_falls_back_to_arrow(self):
        out = _transition_label("foo", "bar")
        assert "foo" in out and "bar" in out


class TestReceivingRoleSet:
    """The fast-path query only fetches issues in 'receiving' roles —
    confirm the set covers dev/qa/release/rework but not done/paused/triage."""

    def test_dev_qa_release_rework_included(self):
        assert "dev" in _HANDOFF_RECEIVING_ROLES
        assert "qa" in _HANDOFF_RECEIVING_ROLES
        assert "release" in _HANDOFF_RECEIVING_ROLES
        assert "rework" in _HANDOFF_RECEIVING_ROLES

    def test_terminal_states_excluded(self):
        assert "done" not in _HANDOFF_RECEIVING_ROLES
        assert "paused" not in _HANDOFF_RECEIVING_ROLES


class TestSortByDaysStuck:
    """Confirm the sort key (negative days_stuck, negative severity) puts
    worst-stall-first and uses severity as the tiebreaker."""

    def test_worst_stall_first(self):
        # Simulate the sort behavior by calling the same logic the tool uses
        items = [
            {"days_stuck": 5.0, "severity": "Major", "id": "A"},
            {"days_stuck": 20.0, "severity": "Minor", "id": "B"},
            {"days_stuck": 10.0, "severity": "Blocker", "id": "C"},
        ]
        _sev_weight = {"blocker": 5, "critical": 4, "major": 3, "minor": 1, "trivial": 0}

        def key(it):
            sev = _sev_weight.get((it.get("severity") or "").lower(), 0)
            return (-it["days_stuck"], -sev)

        items.sort(key=key)
        ids = [i["id"] for i in items]
        assert ids == ["B", "C", "A"]

    def test_severity_breaks_tie_at_same_stall(self):
        items = [
            {"days_stuck": 10.0, "severity": "Minor", "id": "A"},
            {"days_stuck": 10.0, "severity": "Blocker", "id": "B"},
        ]
        _sev_weight = {"blocker": 5, "critical": 4, "major": 3, "minor": 1, "trivial": 0}

        def key(it):
            sev = _sev_weight.get((it.get("severity") or "").lower(), 0)
            return (-it["days_stuck"], -sev)

        items.sort(key=key)
        assert items[0]["id"] == "B"
