"""Guards for the contract module — one test per encoded quirk (ADR-044).

These are the tripwires the quirk registry points at. Each asserts the
CORRECT form is produced and, where the broken form is what a naive
implementation would emit, that it is not.
"""

from datetime import datetime, timezone

import pytest

from yt_mcp import contract


NOW_MS = int(datetime(2026, 8, 17, tzinfo=timezone.utc).timestamp() * 1000)


class TestDateRange:
    """Q1: `resolved:` alias is broken for ranges; absolute ISO dates only."""

    def test_absolute_iso_range(self):
        assert contract.build_date_range(30, NOW_MS) == "2026-07-18 .. 2026-08-17"

    def test_no_relative_offset_forms(self):
        clause = contract.build_date_range(30, NOW_MS)
        assert "-30d" not in clause and "*" not in clause
        assert clause.count("..") == 1

    def test_canonical_attribute_is_not_the_alias(self):
        assert contract.RESOLVED_DATE_ATTR == "resolved date:"
        assert not contract.RESOLVED_DATE_ATTR.startswith("resolved:")


class TestClauseOrder:
    """Q2: the range clause must come FIRST in a composed query."""

    def test_range_leads_the_query(self):
        q = contract.resolved_window_query(30, "summary: Release", now_ms=NOW_MS)
        assert q.startswith("resolved date: 2026-07-18 .. 2026-08-17")
        assert q.endswith("summary: Release")

    def test_empty_tail_is_clean(self):
        q = contract.resolved_window_query(7, now_ms=NOW_MS)
        assert q == "resolved date: 2026-08-10 .. 2026-08-17"

    def test_blank_clauses_dropped(self):
        q = contract.resolved_window_query(7, "", "  ", "project: PROJ", now_ms=NOW_MS)
        assert q.endswith("project: PROJ")
        assert "  " not in q.replace(" .. ", "")


class TestProjectClause:
    """Q17: comma-list, never OR-joined same-prefix clauses."""

    def test_comma_list_form(self):
        assert contract.project_clause("PROJ, OPS") == "project: PROJ, OPS"

    def test_never_emits_or(self):
        assert " OR " not in contract.project_clause(["PROJ", "OPS", "DEMO"])

    def test_list_and_string_agree(self):
        assert contract.project_clause("PROJ,OPS") == contract.project_clause(["PROJ", "OPS"])

    def test_empty_is_empty_string(self):
        assert contract.project_clause("") == ""
        assert contract.project_clause([" ", ""]) == ""

    def test_values_escaped(self):
        assert "{" not in contract.project_clause("{PROJ}")


class TestLinkedState:
    """Q3: linked issues have no top-level `state`; read the custom field."""

    def test_reads_custom_field_when_no_top_level(self):
        linked = {"customFields": [{"name": "State", "value": {"name": "On testing"}}]}
        assert contract.linked_state(linked) == "On testing"

    def test_top_level_wins_when_present(self):
        linked = {"state": {"name": "Open"},
                  "customFields": [{"name": "State", "value": {"name": "Closed"}}]}
        assert contract.linked_state(linked) == "Open"

    def test_absent_degrades_to_empty(self):
        assert contract.linked_state({"idReadable": "PROJ-1"}) == ""


class TestCustomField:
    """Q9: exact field names, including emoji-decorated ones."""

    def test_emoji_name_matched_exactly(self):
        issue = {"customFields": [{"name": "Evaluation time 🕙",
                                   "value": {"name": "3d"}}]}
        assert contract.custom_field(issue, "Evaluation time 🕙") == "3d"
        assert contract.custom_field(issue, "Evaluation time") is None

    def test_multi_value_joined(self):
        issue = {"customFields": [{"name": "Assignee",
                                   "value": [{"name": "Alice A"}, {"name": "Bob B"}]}]}
        assert contract.custom_field(issue, "Assignee") == "Alice A, Bob B"

    def test_missing_field_is_none(self):
        assert contract.custom_field({"customFields": []}, "State") is None


class TestServiceComments:
    """Q10: filter by login prefix and text stamp, never display name."""

    def test_bot_login_and_service_stamp_hidden(self):
        real, hidden = contract.split_service_comments([
            {"text": "human note", "author": {"login": "alice_a", "name": "Alice A"}},
            {"text": "nag", "author": {"login": "workflow_user_9", "name": "Alice A"}},
            {"text": "[yt-mcp] stamp", "author": {"login": "alice_a"}},
        ])
        assert [c["text"] for c in real] == ["human note"]
        assert hidden == 2

    def test_display_name_is_not_the_key(self):
        # Same display name, human login → kept.
        real, hidden = contract.split_service_comments(
            [{"text": "x", "author": {"login": "bob_b", "name": "Bot"}}])
        assert len(real) == 1 and hidden == 0


class TestReleaseShape:
    """Q11: `summary:` search matches mentions; filter by shape."""

    @pytest.mark.parametrize("summary", [
        "Release 8.5.0", "release-3.5.0", "Release_lite",
        "[Tag] Release 1.1.7", "RC-5.5.3", "Release 2.10.96 (2.10.98)",
    ])
    def test_release_shapes_accepted(self, summary):
        assert contract.is_release_ticket(summary)

    @pytest.mark.parametrize("summary", [
        "Validate payments before release", "Check info after release",
        "Job trigger_release_dev = failed", "Automate release loading", "",
    ])
    def test_mentions_rejected(self, summary):
        assert not contract.is_release_ticket(summary)


class TestCreateFields:
    """Q16: entity-creating POSTs must request a fields selector."""

    def test_appends_selector(self):
        assert contract.with_fields("/api/issues") == "/api/issues?fields=idReadable,summary"

    def test_respects_existing_query_string(self):
        out = contract.with_fields("/api/issues?draftId=1")
        assert out == "/api/issues?draftId=1&fields=idReadable,summary"

    def test_idreadable_always_requested(self):
        assert "idReadable" in contract.CREATE_FIELDS


class TestNoHeavyDependencies:
    """The importable-surface property: stdlib only, no async, no SDK."""

    def test_module_imports_nothing_heavy(self):
        import pathlib
        src = pathlib.Path(contract.__file__).read_text()
        for banned in ("import httpx", "from mcp", "import mcp", "async def"):
            assert banned not in src, f"contract.py must stay dependency-free: {banned}"
