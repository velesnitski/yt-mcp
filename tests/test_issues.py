import pytest
from unittest.mock import AsyncMock, MagicMock

from yt_mcp.tools.issues import _parse_command_fields, _fetch_field_types


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


class TestParseCommandFieldsWithDynamicTypes:
    """Test that dynamically fetched field types override static fallback."""

    def test_dynamic_type_overrides_fallback(self):
        # Subsystem static fallback is OwnedIssueCustomField,
        # but project says it's SingleEnumIssueCustomField
        field_types = {"subsystem": "SingleEnumIssueCustomField"}
        result = _parse_command_fields("Subsystem Frontend", field_types)
        assert result[0]["$type"] == "SingleEnumIssueCustomField"

    def test_dynamic_type_for_unknown_field(self):
        field_types = {"board": "SingleEnumIssueCustomField"}
        result = _parse_command_fields("Board {Frontend Board}", field_types)
        assert result[0]["$type"] == "SingleEnumIssueCustomField"
        assert result[0]["value"] == {"name": "Frontend Board"}

    def test_fallback_when_field_not_in_dynamic(self):
        field_types = {"priority": "SingleEnumIssueCustomField"}
        result = _parse_command_fields("Subsystem Frontend", field_types)
        # Subsystem not in dynamic → uses static fallback
        assert result[0]["$type"] == "OwnedIssueCustomField"

    def test_empty_dynamic_uses_fallback(self):
        result = _parse_command_fields("Subsystem Frontend", {})
        assert result[0]["$type"] == "OwnedIssueCustomField"

    def test_none_dynamic_uses_fallback(self):
        result = _parse_command_fields("Subsystem Frontend", None)
        assert result[0]["$type"] == "OwnedIssueCustomField"


class TestFetchFieldTypes:
    @pytest.mark.asyncio
    async def test_parses_project_fields(self):
        client = MagicMock()
        client.get = AsyncMock(return_value=[
            {"field": {"name": "Subsystem"}, "$type": "EnumProjectCustomField"},
            {"field": {"name": "Type"}, "$type": "EnumProjectCustomField"},
            {"field": {"name": "State"}, "$type": "StateProjectCustomField"},
            {"field": {"name": "Assignee"}, "$type": "UserProjectCustomField"},
        ])
        result = await _fetch_field_types(client, "0-3")
        assert result["subsystem"] == "SingleEnumIssueCustomField"
        assert result["type"] == "SingleEnumIssueCustomField"
        assert result["state"] == "StateIssueCustomField"
        assert result["assignee"] == "SingleUserIssueCustomField"

    @pytest.mark.asyncio
    async def test_owned_field(self):
        client = MagicMock()
        client.get = AsyncMock(return_value=[
            {"field": {"name": "Subsystem"}, "$type": "OwnedProjectCustomField"},
        ])
        result = await _fetch_field_types(client, "0-3")
        assert result["subsystem"] == "OwnedIssueCustomField"

    @pytest.mark.asyncio
    async def test_api_error_returns_empty(self):
        client = MagicMock()
        client.get = AsyncMock(side_effect=ValueError("403 Forbidden"))
        result = await _fetch_field_types(client, "0-3")
        assert result == {}

    @pytest.mark.asyncio
    async def test_case_insensitive_keys(self):
        client = MagicMock()
        client.get = AsyncMock(return_value=[
            {"field": {"name": "Priority"}, "$type": "EnumProjectCustomField"},
        ])
        result = await _fetch_field_types(client, "0-3")
        assert "priority" in result

    @pytest.mark.asyncio
    async def test_unknown_project_type_defaults(self):
        client = MagicMock()
        client.get = AsyncMock(return_value=[
            {"field": {"name": "Custom"}, "$type": "SomeFutureProjectCustomField"},
        ])
        result = await _fetch_field_types(client, "0-3")
        assert result["custom"] == "SingleEnumIssueCustomField"
