from yt_mcp.tools.issues import _parse_command_fields


class TestParseCommandFields:
    def test_single_braced_value(self):
        result = _parse_command_fields("Subsystem {Client Panel}")
        assert len(result) == 1
        assert result[0]["name"] == "Subsystem"
        assert result[0]["value"] == {"name": "Client Panel"}
        assert result[0]["$type"] == "OwnedIssueCustomField"

    def test_single_word_value(self):
        result = _parse_command_fields("Type Bug")
        assert len(result) == 1
        assert result[0]["name"] == "Type"
        assert result[0]["value"] == {"name": "Bug"}
        assert result[0]["$type"] == "SingleEnumIssueCustomField"

    def test_multiple_fields(self):
        result = _parse_command_fields("Type Task Subsystem {Client Panel}")
        assert len(result) == 2
        names = {f["name"] for f in result}
        assert names == {"Type", "Subsystem"}

    def test_priority_type(self):
        result = _parse_command_fields("Priority Critical")
        assert result[0]["$type"] == "SingleEnumIssueCustomField"

    def test_state_type(self):
        result = _parse_command_fields("State {In Progress}")
        assert result[0]["$type"] == "StateIssueCustomField"

    def test_assignee_type(self):
        result = _parse_command_fields("Assignee john.doe")
        assert result[0]["$type"] == "SingleUserIssueCustomField"

    def test_unknown_field_defaults_to_single_enum(self):
        result = _parse_command_fields("CustomField Value")
        assert result[0]["$type"] == "SingleEnumIssueCustomField"

    def test_tag_keyword_skipped(self):
        result = _parse_command_fields("tag urgent")
        assert len(result) == 0

    def test_untag_keyword_skipped(self):
        result = _parse_command_fields("untag obsolete")
        assert len(result) == 0

    def test_mixed_fields_and_keywords(self):
        result = _parse_command_fields("Type Bug tag urgent Subsystem {Client Panel}")
        assert len(result) == 2
        names = {f["name"] for f in result}
        assert names == {"Type", "Subsystem"}

    def test_empty_command(self):
        assert _parse_command_fields("") == []

    def test_braced_then_simple(self):
        result = _parse_command_fields("Subsystem {Client Panel} Priority High")
        assert len(result) == 2
        assert result[0]["name"] == "Subsystem"
        assert result[0]["value"] == {"name": "Client Panel"}
        assert result[1]["name"] == "Priority"
        assert result[1]["value"] == {"name": "High"}
