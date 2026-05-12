"""Tests for deadline control tools. All fixtures use generic names."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yt_mcp.tools import deadlines
from yt_mcp.tools.deadlines import config as dcfg


# ---------- pure helpers ----------

class TestQuarterParsing:
    def test_q1(self):
        start, end = deadlines._quarter_to_range("2026Q1")
        assert start == datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert end.month == 3 and end.day == 31

    def test_q4(self):
        start, end = deadlines._quarter_to_range("2026Q4")
        assert start.month == 10
        assert end.year == 2026 and end.month == 12 and end.day == 31

    def test_invalid_format(self):
        with pytest.raises(ValueError):
            deadlines._quarter_to_range("2026-Q2")

    def test_current_quarter(self):
        q = deadlines._current_quarter()
        assert deadlines._QUARTER_RE.match(q)


class TestDeadlineFieldDetection:
    def test_english_variants(self):
        assert deadlines._is_deadline_field("Deadline")
        assert deadlines._is_deadline_field("Due Date")
        assert deadlines._is_deadline_field("due")

    def test_russian_variants(self):
        assert deadlines._is_deadline_field("Дедлайн")
        assert deadlines._is_deadline_field("Срок")

    def test_unrelated_fields(self):
        assert not deadlines._is_deadline_field("Priority")
        assert not deadlines._is_deadline_field("")
        assert not deadlines._is_deadline_field("Date Created")


class TestDeadlineExtraction:
    def test_extract_from_dict_presentation(self):
        ts = deadlines._extract_deadline_ts({"presentation": "2026-06-15"})
        assert ts == int(
            datetime(2026, 6, 15, tzinfo=timezone.utc).timestamp() * 1000
        )

    def test_extract_from_int(self):
        assert deadlines._extract_deadline_ts(1700000000000) == 1700000000000

    def test_extract_none(self):
        assert deadlines._extract_deadline_ts(None) is None

    def test_extract_invalid(self):
        assert deadlines._extract_deadline_ts({"presentation": "garbage"}) is None

    def test_activity_date_from_list(self):
        ts = deadlines._extract_activity_date([{"presentation": "2026-05-01"}])
        assert ts is not None
        assert deadlines._format_date(ts) == "2026-05-01"

    def test_activity_date_empty(self):
        assert deadlines._extract_activity_date(None) is None
        assert deadlines._extract_activity_date([]) is None


class TestStandupExclusion:
    def test_default_patterns_match_devops_daily(self):
        patterns = deadlines._compile_standup_patterns({})
        assert deadlines._is_standup("DevOps Daily 05.05.26", patterns)

    def test_default_patterns_match_russian(self):
        patterns = deadlines._compile_standup_patterns({})
        assert deadlines._is_standup("Решение текущих проблем 05.05.26", patterns)

    def test_non_standup_not_matched(self):
        patterns = deadlines._compile_standup_patterns({})
        assert not deadlines._is_standup("Fix login page bug", patterns)

    def test_custom_pattern_override(self):
        patterns = deadlines._compile_standup_patterns({"standup_patterns": [r"sync.*meeting"]})
        assert deadlines._is_standup("team sync meeting", patterns)
        assert not deadlines._is_standup("DevOps Daily", patterns)


# ---------- approval classifier ----------

class TestClassifyShift:
    def _base_args(self):
        return {
            "shift_ts": 1700_000_000_000,
            "shift_author": "alice.user",
            "old_ms": 1699_000_000_000,
            "new_ms": 1701_000_000_000,
            "approvers": {"bob.manager"},
            "manual_review": False,
            "comments": [],
            "strict": False,
            "policy_effective_ms": 0,
        }

    def test_pre_policy(self):
        args = self._base_args()
        args["policy_effective_ms"] = 1700_000_000_001  # after the shift
        out = deadlines._classify_shift(**args)
        assert out["classification"] == "pre_policy"

    def test_informational_first_time_set(self):
        args = self._base_args()
        args["old_ms"] = None
        out = deadlines._classify_shift(**args)
        assert out["classification"] == "informational"

    def test_informational_earlier_date(self):
        args = self._base_args()
        args["old_ms"] = 1701_000_000_000
        args["new_ms"] = 1700_000_000_000  # pulling earlier
        out = deadlines._classify_shift(**args)
        assert out["classification"] == "informational"

    def test_approver_unknown_no_mapping(self):
        args = self._base_args()
        args["approvers"] = set()
        out = deadlines._classify_shift(**args)
        assert out["classification"] == "approver_unknown"

    def test_approver_unknown_manual_review(self):
        args = self._base_args()
        args["manual_review"] = True
        out = deadlines._classify_shift(**args)
        assert out["classification"] == "approver_unknown"

    def test_compliant_strict_self_authored(self):
        """If the approver themselves shifts the deadline, that's self-authorized."""
        args = self._base_args()
        args["shift_author"] = "bob.manager"
        out = deadlines._classify_shift(**args)
        assert out["classification"] == "compliant_strict"

    def test_compliant_strict_keyword_and_date(self):
        """Strict bucket: approver comment has keyword AND the new date string."""
        new_date_str = deadlines._format_date(1701_000_000_000)
        args = self._base_args()
        args["comments"] = [{
            "id": "c1",
            "created": 1699_999_999_000,
            "author": {"login": "bob.manager"},
            "text": f"approved, new deadline {new_date_str}",
        }]
        out = deadlines._classify_shift(**args)
        assert out["classification"] == "compliant_strict"

    def test_compliant_loose_keyword_only(self):
        """Approver comment in window without new-date string → loose."""
        args = self._base_args()
        args["comments"] = [{
            "id": "c1",
            "created": 1699_999_999_000,
            "author": {"login": "bob.manager"},
            "text": "ok",
        }]
        out = deadlines._classify_shift(**args)
        assert out["classification"] == "compliant_loose"

    def test_loose_not_promoted_in_strict_mode(self):
        """In strict=True mode, loose evidence is rejected → unauthorized."""
        args = self._base_args()
        args["strict"] = True
        args["comments"] = [{
            "id": "c1", "created": 1699_999_999_000,
            "author": {"login": "bob.manager"}, "text": "ok",
        }]
        out = deadlines._classify_shift(**args)
        assert out["classification"] == "unauthorized"

    def test_unauthorized_no_signal(self):
        out = deadlines._classify_shift(**self._base_args())
        assert out["classification"] == "unauthorized"

    def test_comment_outside_window_ignored(self):
        args = self._base_args()
        # 30 days before; window is 14d before
        args["comments"] = [{
            "id": "c1",
            "created": args["shift_ts"] - 30 * 86400 * 1000,
            "author": {"login": "bob.manager"},
            "text": "approved",
        }]
        out = deadlines._classify_shift(**args)
        assert out["classification"] == "unauthorized"

    def test_comment_from_non_approver_ignored(self):
        args = self._base_args()
        args["comments"] = [{
            "id": "c1", "created": 1699_999_999_000,
            "author": {"login": "random.user"}, "text": "approved",
        }]
        out = deadlines._classify_shift(**args)
        assert out["classification"] == "unauthorized"

    def test_unrelated_approver_comment_does_not_trigger_loose(self):
        """REGRESSION: a comment by an approver with no approval keyword was
        previously misclassified as `compliant_loose` solely because its
        timestamp was before the shift. It must now be `unauthorized`."""
        args = self._base_args()
        args["comments"] = [{
            "id": "c1",
            "created": args["shift_ts"] - 3 * 86400 * 1000,  # 3 days before
            "author": {"login": "bob.manager"},
            "text": "deploying tonight",  # no keyword, unrelated content
        }]
        out = deadlines._classify_shift(**args)
        assert out["classification"] == "unauthorized"


# ---------- config loading ----------

class TestConfigLoading:
    def test_returns_empty_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dcfg, "_MANAGERS_FILE", tmp_path / "missing.json")
        monkeypatch.setattr(dcfg, "_MANAGERS_SUGGESTED_FILE", tmp_path / "missing2.json")
        config, metadata = deadlines._load_managers_config()
        assert config == {}
        assert metadata["source_file"] == ""

    def test_loads_managers_json(self, tmp_path, monkeypatch):
        managers = {
            "__default__": "default.user",
            "alice.user": {"primary": "bob.manager", "also_accept": ["carol.lead"]},
        }
        f = tmp_path / "managers.json"
        f.write_text(json.dumps(managers))
        monkeypatch.setattr(dcfg, "_MANAGERS_FILE", f)
        config, metadata = deadlines._load_managers_config()
        assert config["__default__"] == "default.user"
        assert config["alice.user"]["primary"] == "bob.manager"
        assert metadata["source_file"] == str(f)

    def test_falls_back_to_suggested(self, tmp_path, monkeypatch):
        suggested = {"alice.user": {"primary": "bob.manager", "also_accept": []}}
        f = tmp_path / "managers.suggested.json"
        f.write_text(json.dumps(suggested))
        monkeypatch.setattr(dcfg, "_MANAGERS_FILE", tmp_path / "missing.json")
        monkeypatch.setattr(dcfg, "_MANAGERS_SUGGESTED_FILE", f)
        config, metadata = deadlines._load_managers_config()
        assert config["alice.user"]["primary"] == "bob.manager"
        assert metadata["source_file"] == str(f)

    def test_metadata_stripped_from_user_entries(self, tmp_path, monkeypatch):
        """The `_metadata` key is split out — never bleeds into user-entry view."""
        suggested = {
            "_metadata": {"generated": "2026-05-12T00:00:00+00:00", "pms_excluded": ["pm.olga"]},
            "alice.user": {"primary": "bob.manager"},
        }
        f = tmp_path / "managers.suggested.json"
        f.write_text(json.dumps(suggested))
        monkeypatch.setattr(dcfg, "_MANAGERS_FILE", tmp_path / "missing.json")
        monkeypatch.setattr(dcfg, "_MANAGERS_SUGGESTED_FILE", f)
        config, metadata = deadlines._load_managers_config()
        assert "_metadata" not in config
        assert config["alice.user"]["primary"] == "bob.manager"
        assert metadata["pms_excluded"] == ["pm.olga"]


class TestGetApprovers:
    def test_returns_primary_and_also_accept(self):
        cfg = {
            "alice.user": {
                "primary": "bob.manager",
                "also_accept": ["carol.lead"],
            },
        }
        approvers, manual = deadlines._get_approvers("alice.user", cfg)
        assert approvers == {"bob.manager", "carol.lead"}
        assert manual is False

    def test_falls_back_to_default(self):
        cfg = {"__default__": "default.user"}
        approvers, _ = deadlines._get_approvers("unknown.user", cfg)
        assert approvers == {"default.user"}

    def test_returns_empty_when_no_default(self):
        approvers, _ = deadlines._get_approvers("unknown.user", {})
        assert approvers == set()

    def test_manual_review_flag(self):
        cfg = {"alice.user": {"primary": None, "also_accept": [], "manual_review": True}}
        approvers, manual = deadlines._get_approvers("alice.user", cfg)
        assert manual is True

    def test_get_reports_reverse_lookup(self):
        cfg = {
            "alice.user": {"primary": "bob.manager"},
            "carol.user": {"primary": "bob.manager"},
            "dave.user": {"primary": "other.manager"},
        }
        assert deadlines._get_reports("bob.manager", cfg) == ["alice.user", "carol.user"]


# ---------- tool wiring (end-to-end with mock client) ----------

def _make_client(activities_by_issue=None, comments_by_issue=None,
                  issues_list=None, current_user="ops.alice"):
    """Build a mock YouTrack client that returns canned responses."""
    activities_by_issue = activities_by_issue or {}
    comments_by_issue = comments_by_issue or {}
    issues_list = issues_list or []

    async def fake_get(path, params=None):
        if path == "/api/users/me":
            return {"login": current_user}
        if path == "/api/issues":
            return issues_list
        if path.startswith("/api/issues/") and path.endswith("/activities"):
            iid = path.split("/")[3]
            return activities_by_issue.get(iid, [])
        if path.startswith("/api/issues/") and path.endswith("/comments"):
            iid = path.split("/")[3]
            return comments_by_issue.get(iid, [])
        return []

    mock = MagicMock()
    mock.get = AsyncMock(side_effect=fake_get)
    return mock


def _make_resolver(client):
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    return resolver


def _register_and_get(client):
    """Register deadlines tools on a fake mcp and return the {name: fn} map."""
    tools = {}

    class FakeMcp:
        def tool(self):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn
            return decorator

    deadlines.register(FakeMcp(), _make_resolver(client))
    return tools


class TestAuditTool:
    def test_no_issues_returns_empty_message(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dcfg, "_CONFIG_DIR", tmp_path)
        monkeypatch.setattr(dcfg, "_MANAGERS_FILE", tmp_path / "managers.json")
        monkeypatch.setattr(dcfg, "_MANAGERS_SUGGESTED_FILE", tmp_path / "managers.suggested.json")
        monkeypatch.setattr(dcfg, "_AUDIT_LOG", tmp_path / "audit.log")
        client = _make_client(issues_list=[])
        tools = _register_and_get(client)

        import asyncio
        out = asyncio.run(tools["audit_deadline_changes"](
            period_start="2026-04-01", period_finish="2026-06-30",
        ))
        assert "No issues match query" in out

    def test_unauthorized_shift_classified(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dcfg, "_CONFIG_DIR", tmp_path)
        # Write a managers config so the assignee has an approver mapping
        cfg = {"alice.user": {"primary": "bob.manager", "also_accept": []}}
        f = tmp_path / "managers.json"
        f.write_text(json.dumps(cfg))
        monkeypatch.setattr(dcfg, "_MANAGERS_FILE", f)
        monkeypatch.setattr(dcfg, "_MANAGERS_SUGGESTED_FILE", tmp_path / "x.json")
        monkeypatch.setattr(dcfg, "_AUDIT_LOG", tmp_path / "audit.log")

        shift_ts = int(datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp() * 1000)
        issues = [{
            "idReadable": "ALPHA-1",
            "summary": "Some real task",
            "reporter": {"login": "carol.pm", "name": "Carol PM"},
            "customFields": [
                {"name": "Assignee", "value": {"login": "alice.user", "name": "Alice"}},
                {"name": "Due Date", "value": {"presentation": "2026-06-20"}},
            ],
        }]
        activities = {"ALPHA-1": [{
            "id": "a1",
            "timestamp": shift_ts,
            "author": {"login": "alice.user", "name": "Alice"},
            "field": {"name": "Due Date"},
            "removed": [{"presentation": "2026-05-15"}],
            "added": [{"presentation": "2026-06-20"}],
        }]}
        comments = {"ALPHA-1": []}  # no approval
        client = _make_client(activities, comments, issues)
        tools = _register_and_get(client)

        import asyncio
        out = asyncio.run(tools["audit_deadline_changes"](
            period_start="2026-04-01", period_finish="2026-06-30",
        ))
        assert "unauthorized" in out
        assert "ALPHA-1" in out
        assert "alice.user" in out

    def test_standup_excluded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dcfg, "_CONFIG_DIR", tmp_path)
        monkeypatch.setattr(dcfg, "_MANAGERS_FILE", tmp_path / "m.json")
        monkeypatch.setattr(dcfg, "_MANAGERS_SUGGESTED_FILE", tmp_path / "s.json")
        monkeypatch.setattr(dcfg, "_AUDIT_LOG", tmp_path / "a.log")
        issues = [{
            "idReadable": "ALPHA-2",
            "summary": "DevOps Daily 05.05.26",
            "reporter": {"login": "carol.pm"},
            "customFields": [{"name": "Assignee", "value": {"login": "alice.user"}}],
        }]
        client = _make_client({}, {}, issues)
        tools = _register_and_get(client)
        import asyncio
        out = asyncio.run(tools["audit_deadline_changes"](
            period_start="2026-04-01", period_finish="2026-06-30",
            exclude_standups=True,
        ))
        # No shifts because issue was filtered out
        assert "Shifts found:** 0" in out


class TestScorecardTool:
    def test_missed_deadline_counted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dcfg, "_CONFIG_DIR", tmp_path)
        cfg = {"alice.user": {"primary": "bob.manager", "also_accept": []}}
        f = tmp_path / "managers.json"
        f.write_text(json.dumps(cfg))
        monkeypatch.setattr(dcfg, "_MANAGERS_FILE", f)
        monkeypatch.setattr(dcfg, "_MANAGERS_SUGGESTED_FILE", tmp_path / "x.json")
        monkeypatch.setattr(dcfg, "_AUDIT_LOG", tmp_path / "a.log")
        # Issue with deadline in past, state != Done
        issues = [{
            "idReadable": "ALPHA-1",
            "summary": "Task",
            "state": {"name": "In Progress"},
            "reporter": {"login": "carol.pm"},
            "customFields": [
                {"name": "Assignee", "value": {"login": "alice.user"}},
                {"name": "Due Date", "value": {"presentation": "2026-04-15"}},
            ],
        }]
        client = _make_client({}, {}, issues)
        tools = _register_and_get(client)
        import asyncio
        out = asyncio.run(tools["deadline_scorecard"](quarter="2026Q2"))
        # ALPHA-1 due 2026-04-15 is in Q2; we're at 2026-05-12, state In Progress → missed_no_extension
        assert "alice.user" in out

    def test_miss_attribution_is_per_issue(self, tmp_path, monkeypatch):
        """REGRESSION: a compliant shift on ISSUE-1 must not cause the miss on
        ISSUE-2 to be reclassified as missed_after_extension. Previously the
        scorecard used a per-user cumulative counter, silently under-counting
        penalties whenever the user had any prior compliant shift."""
        monkeypatch.setattr(dcfg, "_CONFIG_DIR", tmp_path)
        cfg_data = {"alice.user": {"primary": "bob.manager", "also_accept": []}}
        f = tmp_path / "managers.json"
        f.write_text(json.dumps(cfg_data))
        monkeypatch.setattr(dcfg, "_MANAGERS_FILE", f)
        monkeypatch.setattr(dcfg, "_MANAGERS_SUGGESTED_FILE", tmp_path / "x.json")
        monkeypatch.setattr(dcfg, "_AUDIT_LOG", tmp_path / "a.log")

        # ISSUE-1: compliant_strict shift (Bob, the approver, did it himself)
        # ISSUE-2: missed deadline, never had a shift. Should be missed_no_extension.
        shift_ts = int(datetime(2026, 4, 20, tzinfo=timezone.utc).timestamp() * 1000)
        issues = [
            {
                "idReadable": "ALPHA-1", "summary": "Task with approved shift",
                "state": {"name": "In Progress"},
                "reporter": {"login": "carol.pm"},
                "customFields": [
                    {"name": "Assignee", "value": {"login": "alice.user"}},
                    {"name": "Due Date", "value": {"presentation": "2026-06-20"}},
                ],
            },
            {
                "idReadable": "ALPHA-2", "summary": "Task that just got missed",
                "state": {"name": "In Progress"},
                "reporter": {"login": "carol.pm"},
                "customFields": [
                    {"name": "Assignee", "value": {"login": "alice.user"}},
                    {"name": "Due Date", "value": {"presentation": "2026-04-15"}},
                ],
            },
        ]
        activities = {
            "ALPHA-1": [{
                "id": "a1", "timestamp": shift_ts,
                "author": {"login": "bob.manager"},
                "field": {"name": "Due Date"},
                "removed": [{"presentation": "2026-05-15"}],
                "added": [{"presentation": "2026-06-20"}],
            }],
            "ALPHA-2": [],
        }
        client = _make_client(activities, {}, issues)
        tools = _register_and_get(client)
        import asyncio
        out = asyncio.run(tools["deadline_scorecard"](quarter="2026Q2"))
        # The miss on ALPHA-2 must be `missed_no_extension`, not
        # `missed_after_extension`, because ALPHA-2 itself had no compliant shift.
        assert "Missed (no approved extension):** 1" in out
        assert "Missed after extension:" not in out


class TestSuggesterTool:
    def test_writes_suggested_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dcfg, "_CONFIG_DIR", tmp_path)
        monkeypatch.setattr(dcfg, "_MANAGERS_FILE", tmp_path / "m.json")
        suggested_path = tmp_path / "managers.suggested.json"
        monkeypatch.setattr(dcfg, "_MANAGERS_SUGGESTED_FILE", suggested_path)
        monkeypatch.setattr(dcfg, "_AUDIT_LOG", tmp_path / "audit.log")

        # alice.user has bob.manager as someone who edits her priorities + resolves her tasks
        issues = [
            {
                "idReadable": "ALPHA-1", "summary": "Task 1",
                "reporter": {"login": "carol.pm"},
                "customFields": [{"name": "Assignee", "value": {"login": "alice.user"}}],
            },
            {
                "idReadable": "ALPHA-2", "summary": "Task 2",
                "reporter": {"login": "carol.pm"},
                "customFields": [{"name": "Assignee", "value": {"login": "alice.user"}}],
            },
        ]
        activities = {
            "ALPHA-1": [
                {"field": {"name": "Priority"}, "author": {"login": "bob.manager"},
                 "added": [{"name": "High"}], "removed": [{"name": "Normal"}]},
                {"field": {"name": "State"}, "author": {"login": "bob.manager"},
                 "added": [{"name": "Done"}], "removed": [{"name": "In Progress"}]},
            ],
            "ALPHA-2": [
                {"field": {"name": "Priority"}, "author": {"login": "bob.manager"},
                 "added": [{"name": "Critical"}], "removed": [{"name": "Normal"}]},
            ],
        }
        client = _make_client(activities, {}, issues)
        tools = _register_and_get(client)

        import asyncio
        out = asyncio.run(tools["suggest_managers"](lookback_days=90))
        assert suggested_path.exists()
        written = json.loads(suggested_path.read_text())
        # alice.user should have bob.manager as primary
        assert written["alice.user"]["primary"] == "bob.manager"
        # carol.pm could be in __pms_excluded__ if fan-out crosses the threshold;
        # with only 1 assignee she shouldn't qualify
        assert "alice.user" in written
        assert "Manager suggester" in out

    def test_no_signal_marks_manual_review(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dcfg, "_CONFIG_DIR", tmp_path)
        monkeypatch.setattr(dcfg, "_MANAGERS_FILE", tmp_path / "m.json")
        suggested_path = tmp_path / "managers.suggested.json"
        monkeypatch.setattr(dcfg, "_MANAGERS_SUGGESTED_FILE", suggested_path)
        monkeypatch.setattr(dcfg, "_AUDIT_LOG", tmp_path / "audit.log")

        issues = [{
            "idReadable": "ALPHA-1", "summary": "Lone task",
            "reporter": {"login": "carol.pm"},
            "customFields": [{"name": "Assignee", "value": {"login": "alice.user"}}],
        }]
        client = _make_client({}, {}, issues)
        tools = _register_and_get(client)
        import asyncio
        asyncio.run(tools["suggest_managers"](lookback_days=90))
        written = json.loads(suggested_path.read_text())
        assert written["alice.user"]["primary"] is None
        assert written["alice.user"]["manual_review"] is True
