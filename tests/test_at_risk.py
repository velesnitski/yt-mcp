"""Tests for get_at_risk_issues — decorated-field matching + json/category.

The core bug these cover: deadline/estimate/spent fields were matched by
exact lowercased literal (`cf_name in ('deadline','due date','due')`), so
real decorated names like `Deadline ☠️` / `Evaluation time 🕙` /
`Spent time 🚴🏻‍♂️` were never recognized — Overdue was missed entirely
and Unestimated was inflated.
"""

import json
import time

import pytest
from unittest.mock import AsyncMock, MagicMock

from mcp.server.fastmcp import FastMCP

from yt_mcp.resolver import InstanceResolver
from yt_mcp.tools.monitoring import (
    register as _register_monitoring,
    _is_estimate_field,
    _is_spent_field,
    _period_to_minutes,
    _extract_deadline_ts,
    _CATEGORY_ALIASES,
)


DAY_MS = 86400 * 1000


def _ms_days_from_now(days: float) -> int:
    return int((time.time() + days * 86400) * 1000)


# --- Pure field matchers ---------------------------------------------------

class TestEstimateFieldMatcher:
    @pytest.mark.parametrize("name", [
        "Estimate", "Estimation", "Dev Estimate", "Dev Estimation",
        "Evaluation time 🕙", "Evaluation time", "Total Estimate",
    ])
    def test_matches(self, name):
        assert _is_estimate_field(name) is True

    @pytest.mark.parametrize("name", [
        "Estimated", "Estimator", "Spent time 🚴🏻‍♂️", "State", "", "Deadline",
    ])
    def test_rejects(self, name):
        assert _is_estimate_field(name) is False


class TestSpentFieldMatcher:
    @pytest.mark.parametrize("name", [
        "Spent time 🚴🏻‍♂️", "Spent time", "Spent", "Time Spent", "Logged time",
    ])
    def test_matches(self, name):
        assert _is_spent_field(name) is True

    @pytest.mark.parametrize("name", [
        "Spential", "Evaluation time 🕙", "Estimate", "",
    ])
    def test_rejects(self, name):
        assert _is_spent_field(name) is False


class TestPeriodToMinutes:
    def test_dict_with_minutes(self):
        assert _period_to_minutes({"minutes": 480, "presentation": "1d"}) == 480

    def test_raw_int(self):
        assert _period_to_minutes(300) == 300

    def test_dict_without_minutes_returns_zero(self):
        # We deliberately don't parse `presentation` (1d length is project-
        # configurable) — absent minutes means 0, not a guessed value.
        assert _period_to_minutes({"presentation": "1w 2d"}) == 0

    def test_none_and_garbage(self):
        assert _period_to_minutes(None) == 0
        assert _period_to_minutes("nonsense") == 0


class TestExtractDeadlineTs:
    def test_raw_epoch(self):
        assert _extract_deadline_ts(1780488000000) == 1780488000000

    def test_presentation_date(self):
        ts = _extract_deadline_ts({"presentation": "2026-06-03"})
        assert ts is not None
        # round-trips back to the same date
        import datetime as _dt
        assert _dt.datetime.fromtimestamp(ts / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%d") == "2026-06-03"

    def test_bad_presentation_returns_none(self):
        assert _extract_deadline_ts({"presentation": "not-a-date"}) is None

    def test_none(self):
        assert _extract_deadline_ts(None) is None


class TestCategoryAliases:
    @pytest.mark.parametrize("alias,key", [
        ("overdue", "overdue"),
        ("Overdue", "overdue"),
        ("over estimate", "over_estimate"),
        ("overestimate", "over_estimate"),
        ("deadline", "approaching"),
        ("stale", "stalled"),
        ("no estimate", "unestimated"),
    ])
    def test_alias_resolves(self, alias, key):
        assert _CATEGORY_ALIASES.get(alias.strip().lower()) == key


# --- Tool-level: decorated fields + json + category ------------------------

def _issue(**kw) -> dict:
    cfs = list(kw.get("custom_fields", []))
    return {
        "idReadable": kw.get("id", "PROJ-1"),
        "summary": kw.get("summary", "Some task"),
        "state": {"name": kw.get("state", "In Progress")},
        "assignee": {"name": kw.get("assignee", "Alice A")},
        "priority": {"name": kw.get("priority", "Normal")},
        "updated": kw.get("updated", int(time.time() * 1000)),
        "created": kw.get("created", int(time.time() * 1000)),
        "tags": [],
        "customFields": cfs,
    }


def _deadline_cf(epoch_ms):
    return {"name": "Deadline ☠️", "value": epoch_ms}


def _est_cf(minutes):
    return {"name": "Evaluation time 🕙", "value": {"minutes": minutes, "presentation": "x"}}


def _spent_cf(minutes):
    return {"name": "Spent time 🚴🏻‍♂️", "value": {"minutes": minutes, "presentation": "x"}}


def _make_mcp(issues: list[dict]):
    mcp = FastMCP("test")
    client = MagicMock()
    client.get = AsyncMock(return_value=issues)
    resolver = MagicMock(spec=InstanceResolver)
    resolver.resolve = MagicMock(return_value=client)
    _register_monitoring(mcp, resolver)
    return mcp, client


def _fn(mcp, name):
    return mcp._tool_manager._tools[name].fn


class TestDecoratedDeadlineDetected:
    @pytest.mark.asyncio
    async def test_overdue_with_emoji_field(self):
        # Deadline 10 days in the past via the decorated `Deadline ☠️` field.
        issue = _issue(id="PROJ-1", state="In Progress",
                       custom_fields=[_deadline_cf(_ms_days_from_now(-10))])
        mcp, _ = _make_mcp([issue])
        out = await _fn(mcp, "get_at_risk_issues")(
            project="PROJ", category="overdue", format="json",
        )
        payload = json.loads(out)
        assert payload["categories"]["overdue"]["count"] == 1
        rec = payload["categories"]["overdue"]["issues"][0]
        assert rec["id"] == "PROJ-1"
        assert "overdue" in rec["detail"].lower()

    @pytest.mark.asyncio
    async def test_pre_fix_literal_miss_is_now_caught(self):
        # Regression guard: the old code did `cf_name in ('deadline',...)`
        # which the emoji name fails. If someone reverts to literal match,
        # this overdue would silently vanish.
        issue = _issue(custom_fields=[_deadline_cf(_ms_days_from_now(-3))])
        mcp, _ = _make_mcp([issue])
        out = await _fn(mcp, "get_at_risk_issues")(project="PROJ", format="json")
        assert json.loads(out)["categories"]["overdue"]["count"] == 1


class TestEstimateNotInflatingUnestimated:
    @pytest.mark.asyncio
    async def test_issue_with_decorated_estimate_not_unestimated(self):
        # Has an Evaluation-time estimate → must NOT show as unestimated.
        issue = _issue(id="PROJ-7", state="In Progress",
                       custom_fields=[_est_cf(480), _spent_cf(120)])
        mcp, _ = _make_mcp([issue])
        out = await _fn(mcp, "get_at_risk_issues")(project="PROJ", format="json")
        payload = json.loads(out)
        assert payload["categories"]["unestimated"]["count"] == 0

    @pytest.mark.asyncio
    async def test_over_estimate_detected(self):
        # Spent (16h) > Estimate (8h) → over_estimate fires.
        issue = _issue(id="PROJ-8", state="In Progress",
                       custom_fields=[_est_cf(480), _spent_cf(960)])
        mcp, _ = _make_mcp([issue])
        out = await _fn(mcp, "get_at_risk_issues")(
            project="PROJ", category="over_estimate", format="json",
        )
        payload = json.loads(out)
        assert payload["categories"]["over_estimate"]["count"] == 1
        assert "200%" in payload["categories"]["over_estimate"]["issues"][0]["detail"]

    @pytest.mark.asyncio
    async def test_genuinely_unestimated_still_flagged(self):
        issue = _issue(id="PROJ-9", state="In Progress", custom_fields=[])
        mcp, _ = _make_mcp([issue])
        out = await _fn(mcp, "get_at_risk_issues")(project="PROJ", format="json")
        assert json.loads(out)["categories"]["unestimated"]["count"] == 1


class TestJsonShapeAndCategoryFilter:
    @pytest.mark.asyncio
    async def test_json_top_level_keys(self):
        issue = _issue(custom_fields=[_deadline_cf(_ms_days_from_now(-5))])
        mcp, _ = _make_mcp([issue])
        out = await _fn(mcp, "get_at_risk_issues")(project="PROJ", format="json")
        payload = json.loads(out)
        assert set(payload) >= {"project", "total_at_risk", "thresholds", "categories"}
        assert payload["project"] == "PROJ"
        assert "deadline_warning_days" in payload["thresholds"]

    @pytest.mark.asyncio
    async def test_category_filter_returns_only_that_bucket(self):
        issue = _issue(custom_fields=[_deadline_cf(_ms_days_from_now(-5))])
        mcp, _ = _make_mcp([issue])
        out = await _fn(mcp, "get_at_risk_issues")(
            project="PROJ", category="overdue", format="json",
        )
        payload = json.loads(out)
        assert list(payload["categories"].keys()) == ["overdue"]
        assert payload["filtered_category"] == "overdue"

    @pytest.mark.asyncio
    async def test_invalid_category_errors_with_help(self):
        mcp, client = _make_mcp([])
        out = await _fn(mcp, "get_at_risk_issues")(project="PROJ", category="bogus")
        assert "Unknown category" in out
        assert "overdue" in out
        # Fails fast — no API call
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_report_mode_still_markdown(self):
        issue = _issue(custom_fields=[_deadline_cf(_ms_days_from_now(-5))])
        mcp, _ = _make_mcp([issue])
        out = await _fn(mcp, "get_at_risk_issues")(project="PROJ")
        assert out.startswith("# At Risk Issues")
        assert "Overdue" in out

    @pytest.mark.asyncio
    async def test_category_filter_in_report_header(self):
        issue = _issue(custom_fields=[_deadline_cf(_ms_days_from_now(-5))])
        mcp, _ = _make_mcp([issue])
        out = await _fn(mcp, "get_at_risk_issues")(project="PROJ", category="overdue")
        assert "Overdue" in out


class TestBareFieldNamesRegression:
    @pytest.mark.asyncio
    async def test_plain_deadline_field_still_works(self):
        # Projects with an undecorated "Due Date" must keep working.
        issue = _issue(custom_fields=[
            {"name": "Due Date", "value": _ms_days_from_now(-4)},
        ])
        mcp, _ = _make_mcp([issue])
        out = await _fn(mcp, "get_at_risk_issues")(project="PROJ", format="json")
        assert json.loads(out)["categories"]["overdue"]["count"] == 1


# --- QA-skip compliance signal --------------------------------------------

from yt_mcp.tools.monitoring import (
    _is_qa_required_field, _qa_required_affirmative, _passed_qa_state,
)


def _qa_cf(value="Yes"):
    return {"name": "QA Required", "value": {"name": value}}


def _state_act(from_state, to_state):
    return {
        "field": {"name": "State"},
        "added": [{"name": to_state}],
        "removed": [{"name": from_state}],
    }


def _make_routing_mcp(issues, activities_by_id=None):
    """Mock client that routes the bulk /api/issues query and per-issue
    /activities fetches separately, so the QA-skip history walk is exercised."""
    activities_by_id = activities_by_id or {}
    mcp = FastMCP("test")
    client = MagicMock()

    async def _get(path, params=None):
        if path == "/api/issues":
            return issues
        if path.endswith("/activities"):
            iid = path.split("/")[3]
            return activities_by_id.get(iid, [])
        return {}

    client.get = AsyncMock(side_effect=_get)
    resolver = MagicMock(spec=InstanceResolver)
    resolver.resolve = MagicMock(return_value=client)
    _register_monitoring(mcp, resolver)
    return mcp, client


class TestQaRequiredMatchers:
    @pytest.mark.parametrize("name", [
        "QA Required", "QA Needed", "Requires QA", "Needs QA", "QA Gate",
    ])
    def test_field_matches(self, name):
        assert _is_qa_required_field(name) is True

    @pytest.mark.parametrize("name", ["Quality", "QA Owner", "State", ""])
    def test_field_rejects(self, name):
        assert _is_qa_required_field(name) is False

    @pytest.mark.parametrize("val,expected", [
        ({"name": "Yes"}, True), ({"name": "No"}, False),
        ({"name": "Required"}, True), ("yes", True), (True, True), (False, False),
        ({"name": "Да"}, True), (None, False),
    ])
    def test_affirmative(self, val, expected):
        assert _qa_required_affirmative(val) is expected


class TestPassedQaState:
    def test_true_when_history_has_qa_state(self):
        assert _passed_qa_state([_state_act("In Progress", "On testing")]) is True

    def test_true_when_qa_on_removed_side(self):
        # Was in QA, then moved out — still counts as passed.
        assert _passed_qa_state([_state_act("Ready for test", "Ready for release")]) is True

    def test_false_when_no_qa_state(self):
        assert _passed_qa_state([_state_act("In Progress", "Ready for release")]) is False

    def test_false_on_empty(self):
        assert _passed_qa_state([]) is False

    def test_ignores_non_state_activities(self):
        assert _passed_qa_state([{"field": {"name": "Assignee"}, "added": [{"name": "x"}]}]) is False


class TestQaSkipDetection:
    @pytest.mark.asyncio
    async def test_fires_when_qa_required_release_without_qa_history(self):
        issue = _issue(id="PROJ-1", state="Ready for release", custom_fields=[_qa_cf("Yes")])
        acts = {"PROJ-1": [_state_act("In Progress", "Ready for release")]}
        mcp, _ = _make_routing_mcp([issue], acts)
        out = await _fn(mcp, "get_at_risk_issues")(project="PROJ", format="json")
        payload = json.loads(out)
        assert payload["categories"]["qa_skipped"]["count"] == 1
        rec = payload["categories"]["qa_skipped"]["issues"][0]
        assert rec["id"] == "PROJ-1"
        assert "never entered QA" in rec["detail"]

    @pytest.mark.asyncio
    async def test_does_not_fire_when_qa_in_history(self):
        issue = _issue(id="PROJ-2", state="Ready for release", custom_fields=[_qa_cf("Yes")])
        acts = {"PROJ-2": [
            _state_act("In Progress", "On testing"),
            _state_act("On testing", "Ready for release"),
        ]}
        mcp, _ = _make_routing_mcp([issue], acts)
        out = await _fn(mcp, "get_at_risk_issues")(project="PROJ", format="json")
        assert json.loads(out)["categories"]["qa_skipped"]["count"] == 0

    @pytest.mark.asyncio
    async def test_does_not_fire_when_qa_required_no(self):
        issue = _issue(id="PROJ-3", state="Ready for release", custom_fields=[_qa_cf("No")])
        mcp, client = _make_routing_mcp([issue], {})
        out = await _fn(mcp, "get_at_risk_issues")(project="PROJ", format="json")
        assert json.loads(out)["categories"]["qa_skipped"]["count"] == 0

    @pytest.mark.asyncio
    async def test_does_not_fire_when_not_at_release_gate(self):
        # QA Required=Yes but still in dev — not a candidate (too early).
        issue = _issue(id="PROJ-4", state="In Progress", custom_fields=[_qa_cf("Yes")])
        mcp, client = _make_routing_mcp([issue], {})
        out = await _fn(mcp, "get_at_risk_issues")(project="PROJ", format="json")
        assert json.loads(out)["categories"]["qa_skipped"]["count"] == 0
        # No history walk happened — only the bulk query.
        assert client.get.call_count == 1

    @pytest.mark.asyncio
    async def test_no_qa_field_means_zero_candidates_and_no_history_walk(self):
        # Common-tool: a project without a QA-gating field pays nothing.
        issue = _issue(id="PROJ-5", state="Ready for release", custom_fields=[])
        mcp, client = _make_routing_mcp([issue], {})
        out = await _fn(mcp, "get_at_risk_issues")(project="PROJ", format="json")
        assert json.loads(out)["categories"]["qa_skipped"]["count"] == 0
        assert client.get.call_count == 1  # no /activities calls

    @pytest.mark.asyncio
    async def test_empty_history_is_inconclusive_not_flagged(self):
        issue = _issue(id="PROJ-6", state="Ready for release", custom_fields=[_qa_cf("Yes")])
        mcp, _ = _make_routing_mcp([issue], {"PROJ-6": []})  # no history
        out = await _fn(mcp, "get_at_risk_issues")(project="PROJ", format="json")
        assert json.loads(out)["categories"]["qa_skipped"]["count"] == 0

    @pytest.mark.asyncio
    async def test_category_filter_overdue_skips_qa_history_walk(self):
        # When only 'overdue' is requested, the QA history walk must not run.
        issue = _issue(id="PROJ-7", state="Ready for release", custom_fields=[_qa_cf("Yes")])
        mcp, client = _make_routing_mcp(
            [issue], {"PROJ-7": [_state_act("In Progress", "Ready for release")]},
        )
        await _fn(mcp, "get_at_risk_issues")(project="PROJ", category="overdue", format="json")
        assert client.get.call_count == 1  # bulk only, no /activities

    @pytest.mark.asyncio
    async def test_category_qa_alias_and_filter(self):
        issue = _issue(id="PROJ-8", state="Done", custom_fields=[_qa_cf("Yes")])
        acts = {"PROJ-8": [_state_act("In Progress", "Done")]}
        mcp, _ = _make_routing_mcp([issue], acts)
        out = await _fn(mcp, "get_at_risk_issues")(project="PROJ", category="qa", format="json")
        payload = json.loads(out)
        assert list(payload["categories"].keys()) == ["qa_skipped"]
        assert payload["categories"]["qa_skipped"]["count"] == 1
        assert payload["filtered_category"] == "qa_skipped"
