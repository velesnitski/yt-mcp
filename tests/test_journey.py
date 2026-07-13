"""Tests for cross-department journey tracking."""
from yt_mcp.tools.journey import (
    _detect_dept,
    _state_dept,
    _build_journey,
    _gather_subtask_ids,
)

DAY_MS = 86400 * 1000


class TestDetectDept:
    def test_exact_match(self):
        assert _detect_dept("SRV") == "Backend"
        assert _detect_dept("QA") == "QA"
        assert _detect_dept("DO") == "DevOps"

    def test_case_insensitive(self):
        assert _detect_dept("bac") == "Backend"
        assert _detect_dept("Bac") == "Backend"

    def test_prefix_match(self):
        assert _detect_dept("BACKEND") == "Backend"
        assert _detect_dept("FRONTEND") == "Frontend"
        assert _detect_dept("DEVOPS") == "DevOps"

    def test_mobile_variants(self):
        assert _detect_dept("ANDROID") == "Mobile"
        assert _detect_dept("IOS") == "Mobile"
        assert _detect_dept("APP") == "Mobile"

    def test_unknown_falls_back_to_literal(self):
        assert _detect_dept("XYZ") == "XYZ"
        assert _detect_dept("WEIRDPROJ") == "WEIRDPROJ"

    def test_empty(self):
        assert _detect_dept("") == "Unknown"


class TestStateDept:
    def test_review_states(self):
        assert _state_dept("For Review") == "Review"
        assert _state_dept("Code Review") == "Review"

    def test_qa_states(self):
        assert _state_dept("Dev QA") == "QA"
        assert _state_dept("Staging QA") == "QA"
        assert _state_dept("On Testing") == "QA"

    def test_devops_states(self):
        assert _state_dept("Ready for Stage") == "DevOps"
        assert _state_dept("Ready to Prod") == "DevOps"
        assert _state_dept("Ready for Release") == "DevOps"

    def test_blocked(self):
        assert _state_dept("Blocked") == "Blocked"

    def test_case_insensitive(self):
        assert _state_dept("DEV QA") == "QA"
        assert _state_dept("for review") == "Review"

    def test_no_hint(self):
        assert _state_dept("In Progress") is None
        assert _state_dept("Submitted") is None
        assert _state_dept("") is None


class TestGatherSubtaskIds:
    def test_no_links(self):
        assert _gather_subtask_ids({}) == []

    def test_outward_subtask(self):
        issue = {
            "links": [{
                "direction": "OUTWARD",
                "linkType": {"name": "Subtask"},
                "issues": [{"idReadable": "PROJ-1"}, {"idReadable": "PROJ-2"}],
            }],
        }
        assert _gather_subtask_ids(issue) == ["PROJ-1", "PROJ-2"]

    def test_inward_subtask_ignored(self):
        # INWARD = "I am a subtask of X" — don't follow upward
        issue = {
            "links": [{
                "direction": "INWARD",
                "linkType": {"name": "Subtask"},
                "issues": [{"idReadable": "PROJ-1"}],
            }],
        }
        assert _gather_subtask_ids(issue) == []

    def test_other_link_types_ignored(self):
        issue = {
            "links": [
                {"direction": "OUTWARD", "linkType": {"name": "Relates"},
                 "issues": [{"idReadable": "PROJ-1"}]},
                {"direction": "OUTWARD", "linkType": {"name": "Depend"},
                 "issues": [{"idReadable": "PROJ-2"}]},
            ],
        }
        assert _gather_subtask_ids(issue) == []


class TestBuildJourney:
    def _issue(self, project="SRV", state="Submitted", created=0):
        return {
            "idReadable": "SRV-1",
            "project": {"shortName": project},
            "state": {"name": state},
            "created": created,
        }

    def test_no_activities(self):
        # Just initial event from creation
        events = _build_journey(self._issue(created=0), [], now_ms=10 * DAY_MS)
        assert len(events) == 1
        assert events[0]["dept"] == "Backend"
        assert events[0]["duration_days"] == 10

    def test_state_change_to_review(self):
        # Backend → Review hop via state change
        acts = [{
            "timestamp": 5 * DAY_MS,
            "field": {"name": "state"},
            "added": [{"name": "For Review"}],
        }]
        events = _build_journey(self._issue(created=0), acts, now_ms=10 * DAY_MS)
        assert len(events) == 2
        assert events[0]["dept"] == "Backend"
        assert events[0]["duration_days"] == 5
        assert events[1]["dept"] == "Review"
        assert events[1]["duration_days"] == 5

    def test_full_chain_backend_qa_devops(self):
        acts = [
            {"timestamp": 3 * DAY_MS, "field": {"name": "state"},
             "added": [{"name": "Dev QA"}]},
            {"timestamp": 7 * DAY_MS, "field": {"name": "state"},
             "added": [{"name": "Ready for Stage"}]},
        ]
        events = _build_journey(self._issue(created=0), acts, now_ms=12 * DAY_MS)
        assert [e["dept"] for e in events] == ["Backend", "QA", "DevOps"]
        assert [e["duration_days"] for e in events] == [3, 4, 5]

    def test_state_change_with_no_dept_hint_falls_back_to_project(self):
        # In Progress has no semantic dept, so dept stays = project's dept
        acts = [{
            "timestamp": 5 * DAY_MS,
            "field": {"name": "state"},
            "added": [{"name": "In Progress"}],
        }]
        events = _build_journey(self._issue(created=0), acts, now_ms=10 * DAY_MS)
        # No dept change → no new event
        assert len(events) == 1
        assert events[0]["dept"] == "Backend"

    def test_project_move(self):
        acts = [{
            "timestamp": 5 * DAY_MS,
            "field": {"name": "project"},
            "added": [{"shortName": "QA"}],
        }]
        events = _build_journey(self._issue(project="SRV", created=0), acts, now_ms=10 * DAY_MS)
        assert events[0]["dept"] == "Backend"
        assert events[1]["dept"] == "QA"

    def test_assignee_change_does_not_create_event(self):
        # Assignee changes alone don't shift dept
        acts = [{
            "timestamp": 5 * DAY_MS,
            "field": {"name": "assignee"},
            "added": [{"name": "Bob"}],
        }]
        events = _build_journey(self._issue(created=0), acts, now_ms=10 * DAY_MS)
        assert len(events) == 1

    def test_initial_state_uses_state_hint(self):
        # Issue created already in "Dev QA" → initial dept is QA, not Backend
        events = _build_journey(
            self._issue(project="SRV", state="Dev QA", created=0),
            [], now_ms=5 * DAY_MS,
        )
        assert events[0]["dept"] == "QA"

    def test_chronological_sort(self):
        # Activities given out of order — must be sorted
        acts = [
            {"timestamp": 7 * DAY_MS, "field": {"name": "state"},
             "added": [{"name": "Ready for Stage"}]},
            {"timestamp": 3 * DAY_MS, "field": {"name": "state"},
             "added": [{"name": "Dev QA"}]},
        ]
        events = _build_journey(self._issue(created=0), acts, now_ms=12 * DAY_MS)
        assert [e["dept"] for e in events] == ["Backend", "QA", "DevOps"]
