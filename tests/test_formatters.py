from yt_mcp.formatters import (
    _get_custom_field,
    _resolve_state,
    _resolve_priority,
    _resolve_assignee,
    get_product,
    format_issue_list,
    format_issue_detail,
    format_value,
    parse_issue_id,
)


# --- parse_issue_id ---

class TestParseIssueId:
    def test_plain_id(self):
        assert parse_issue_id("PROJ-123") == "PROJ-123"

    def test_url_with_slug(self):
        assert parse_issue_id(
            "https://company.youtrack.cloud/issue/PROJ-123/some-slug"
        ) == "PROJ-123"

    def test_url_without_slug(self):
        assert parse_issue_id(
            "https://company.youtrack.cloud/issue/DEMO-42"
        ) == "DEMO-42"

    def test_whitespace_stripped(self):
        assert parse_issue_id("  OPS-123  ") == "OPS-123"

    def test_url_extracts_before_query(self):
        assert parse_issue_id(
            "https://x.youtrack.cloud/issue/AB-1?tab=comments"
        ) == "AB-1"


# --- _get_custom_field ---

class TestGetCustomField:
    def test_single_enum(self):
        issue = {"customFields": [{"name": "State", "value": {"name": "Open"}}]}
        assert _get_custom_field(issue, "State") == "Open"

    def test_multi_enum(self):
        issue = {"customFields": [
            {"name": "Assignee", "value": [{"name": "John"}, {"name": "Jane"}]}
        ]}
        assert _get_custom_field(issue, "Assignee") == "John, Jane"

    def test_string_value(self):
        issue = {"customFields": [{"name": "Notes", "value": "hello"}]}
        assert _get_custom_field(issue, "Notes") == "hello"

    def test_null_value(self):
        issue = {"customFields": [{"name": "State", "value": None}]}
        assert _get_custom_field(issue, "State") is None

    def test_missing_field(self):
        issue = {"customFields": [{"name": "State", "value": {"name": "Open"}}]}
        assert _get_custom_field(issue, "Priority") is None

    def test_no_custom_fields(self):
        assert _get_custom_field({}, "State") is None

    def test_empty_list_value(self):
        issue = {"customFields": [{"name": "Assignee", "value": []}]}
        assert _get_custom_field(issue, "Assignee") is None


# --- _resolve_state / _resolve_priority / _resolve_assignee ---

class TestResolveState:
    def test_top_level_state(self):
        issue = {"state": {"name": "Open"}}
        assert _resolve_state(issue) == "Open"

    def test_custom_field_fallback(self):
        issue = {"state": None, "customFields": [{"name": "State", "value": {"name": "Submitted"}}]}
        assert _resolve_state(issue) == "Submitted"

    def test_no_state_returns_unknown(self):
        assert _resolve_state({}) == "Unknown"

    def test_top_level_takes_precedence(self):
        issue = {
            "state": {"name": "Open"},
            "customFields": [{"name": "State", "value": {"name": "Submitted"}}],
        }
        assert _resolve_state(issue) == "Open"


class TestResolvePriority:
    def test_top_level(self):
        assert _resolve_priority({"priority": {"name": "High"}}) == "High"

    def test_custom_field_fallback(self):
        issue = {"customFields": [{"name": "Priority", "value": {"name": "Critical"}}]}
        assert _resolve_priority(issue) == "Critical"

    def test_no_priority(self):
        assert _resolve_priority({}) == "?"


class TestResolveAssignee:
    def test_top_level(self):
        assert _resolve_assignee({"assignee": {"name": "John"}}) == "John"

    def test_custom_field_list(self):
        issue = {"customFields": [{"name": "Assignee", "value": [{"name": "Jane"}]}]}
        assert _resolve_assignee(issue) == "Jane"

    def test_no_assignee(self):
        assert _resolve_assignee({}) == "Unassigned"


# --- get_product ---

class TestGetProduct:
    def test_with_product(self):
        issue = {"customFields": [{"name": "Product", "value": {"name": "Alpha"}}]}
        assert get_product(issue) == "Alpha"

    def test_without_product(self):
        assert get_product({}) == ""


# --- format_issue_list ---

class TestFormatIssueList:
    def test_empty_list(self):
        assert format_issue_list([]) == "No issues found."

    def test_single_issue(self):
        issues = [{"idReadable": "TEST-1", "summary": "Fix bug", "state": {"name": "Open"}}]
        result = format_issue_list(issues)
        assert "TEST-1" in result
        assert "[Open]" in result
        assert "Fix bug" in result

    def test_multiple_issues(self):
        issues = [
            {"idReadable": "A-1", "summary": "First"},
            {"idReadable": "A-2", "summary": "Second"},
        ]
        result = format_issue_list(issues)
        assert "A-1" in result
        assert "A-2" in result

    def test_with_product(self):
        issues = [{"idReadable": "A-1", "summary": "X",
                    "customFields": [{"name": "Product", "value": {"name": "Alpha"}}]}]
        result = format_issue_list(issues)
        assert "(Alpha)" in result


# --- format_issue_detail ---

class TestFormatIssueDetail:
    def test_basic_detail(self):
        data = {
            "idReadable": "TEST-1",
            "summary": "Fix login",
            "state": {"name": "Open"},
            "priority": {"name": "High"},
            "assignee": {"name": "John"},
        }
        result = format_issue_detail(data)
        assert "# TEST-1: Fix login" in result
        assert "**State:** Open" in result
        assert "**Priority:** High" in result
        assert "**Assignee:** John" in result

    def test_with_description(self):
        data = {"idReadable": "T-1", "summary": "X", "description": "Details here"}
        result = format_issue_detail(data)
        assert "## Description" in result
        assert "Details here" in result

    def test_with_comments(self):
        data = {
            "idReadable": "T-1",
            "summary": "X",
            "comments": [{"author": {"name": "Alice"}, "text": "Looks good"}],
        }
        result = format_issue_detail(data)
        assert "## Comments (1)" in result
        assert "**Alice:**" in result

    def test_with_tags(self):
        data = {"idReadable": "T-1", "summary": "X",
                "tags": [{"name": "urgent"}, {"name": "v2"}]}
        result = format_issue_detail(data)
        assert "urgent" in result
        assert "v2" in result

    def test_with_links(self):
        data = {
            "idReadable": "T-1",
            "summary": "X",
            "links": [{
                "linkType": {"name": "Depends on"},
                "direction": "OUTWARD",
                "issues": [{"idReadable": "T-2", "summary": "Dep",
                            "state": {"name": "Done"}}],
            }],
        }
        result = format_issue_detail(data)
        assert "## Links" in result
        assert "T-2" in result
        assert "Depends on" in result


# --- format_value ---

class TestFormatValue:
    def test_none(self):
        assert format_value(None) == "(empty)"

    def test_string(self):
        assert format_value("hello") == "hello"

    def test_long_string_truncated(self):
        long = "x" * 300
        assert len(format_value(long)) == 200

    def test_list_with_names(self):
        assert format_value([{"name": "A"}, {"name": "B"}]) == "A, B"

    def test_list_with_text(self):
        assert format_value([{"text": "comment"}]) == "comment"

    def test_empty_list(self):
        assert format_value([]) == "(empty)"
