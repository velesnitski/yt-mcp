import re

from yt_mcp.tools.issues import _CMD_FIELD_RE, _CMD_KEYWORDS


class TestCommandFieldRegex:
    """Test the regex that splits compound commands into field-value pairs."""

    def test_single_braced_value(self):
        matches = list(_CMD_FIELD_RE.finditer("Subsystem {Client Panel}"))
        assert len(matches) == 1
        assert matches[0].group(1) == "Subsystem"
        assert matches[0].group(2) == "Client Panel"

    def test_single_word_value(self):
        matches = list(_CMD_FIELD_RE.finditer("Type Bug"))
        assert len(matches) == 1
        assert matches[0].group(3) == "Type"
        assert matches[0].group(4) == "Bug"

    def test_multiple_fields(self):
        matches = list(_CMD_FIELD_RE.finditer("Type Bug Subsystem {Client Panel}"))
        assert len(matches) == 2
        # First: Type Bug (simple)
        assert (matches[0].group(3), matches[0].group(4)) == ("Type", "Bug")
        # Second: Subsystem {Client Panel} (braced)
        assert (matches[1].group(1), matches[1].group(2)) == ("Subsystem", "Client Panel")

    def test_braced_then_simple(self):
        matches = list(_CMD_FIELD_RE.finditer("Subsystem {Client Panel} Priority High"))
        assert len(matches) == 2
        assert matches[0].group(2) == "Client Panel"
        assert matches[1].group(4) == "High"

    def test_empty_string(self):
        assert list(_CMD_FIELD_RE.finditer("")) == []

    def test_parentheses_in_braces(self):
        matches = list(_CMD_FIELD_RE.finditer("Subsystem {CRM (Admin Panel)}"))
        assert len(matches) == 1
        assert matches[0].group(2) == "CRM (Admin Panel)"


class TestCommandKeywords:
    """Test that tag/untag keywords are filtered when splitting commands."""

    def test_tag_is_keyword(self):
        assert "tag" in _CMD_KEYWORDS

    def test_untag_is_keyword(self):
        assert "untag" in _CMD_KEYWORDS

    def test_field_names_not_keywords(self):
        for name in ("Type", "Priority", "Subsystem", "State", "Assignee"):
            assert name.lower() not in _CMD_KEYWORDS

    def test_keyword_filtering(self):
        """Simulate the filtering logic used in create_issue."""
        command = "Type Bug tag urgent Subsystem {Client Panel}"
        field_commands = []
        for m in _CMD_FIELD_RE.finditer(command):
            name = m.group(1) or m.group(3)
            value = m.group(2) or m.group(4)
            if name.lower() not in _CMD_KEYWORDS:
                field_commands.append(f"{name} {value}")
        assert field_commands == ["Type Bug", "Subsystem Client Panel"]
