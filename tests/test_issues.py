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


# --- normalize_issue: JSON-friendly issue shape ---

import json
import pytest

from yt_mcp.formatters import normalize_issue


def _yt_issue(**kw) -> dict:
    """Build a mock YT issue response with the new (richer) field shape."""
    return {
        "idReadable": kw.get("id", "PROJ-1"),
        "summary": kw.get("summary", "Title"),
        "description": kw.get("description", "Desc"),
        "state": {"name": kw.get("state", "In Progress")},
        "priority": {"name": kw.get("priority", "Medium")},
        "assignee": kw.get("assignee", {"login": "alice.a", "name": "Alice A"}),
        "created": kw.get("created", 1747584000000),
        "updated": kw.get("updated", 1747670400000),
        "resolved": kw.get("resolved"),
        "tags": kw.get("tags", [{"name": "release-blocker"}, {"name": "v2"}]),
        "customFields": kw.get("customFields", [
            {"name": "Severity", "value": {"name": "Major"}},
            {"name": "Type", "value": {"name": "Bug"}},
            {"name": "Deadline ☠️", "value": {"presentation": "2026-05-30"}},
        ]),
        "links": kw.get("links", []),
    }


class TestNormalizeIssueBasics:
    def test_top_level_fields_extracted(self):
        out = normalize_issue(_yt_issue())
        assert out["id"] == "PROJ-1"
        assert out["summary"] == "Title"
        assert out["description"] == "Desc"
        assert out["state"] == "In Progress"
        assert out["priority"] == "Medium"
        assert out["assignee"] == "Alice A"
        assert out["assignee_login"] == "alice.a"
        assert out["created"] == 1747584000000

    def test_tags_normalized_to_string_list(self):
        out = normalize_issue(_yt_issue())
        assert out["tags"] == ["release-blocker", "v2"]

    def test_tags_empty_when_absent(self):
        issue = _yt_issue()
        issue.pop("tags")
        out = normalize_issue(issue)
        assert out["tags"] == []

    def test_custom_fields_dict_shape(self):
        out = normalize_issue(_yt_issue())
        assert out["custom_fields"]["Severity"] == "Major"
        assert out["custom_fields"]["Type"] == "Bug"
        # Deadline uses `presentation` not `name` — handler falls through
        assert out["custom_fields"]["Deadline ☠️"] == "2026-05-30"

    def test_custom_fields_list_value(self):
        issue = _yt_issue(customFields=[
            {"name": "Subsystems", "value": [
                {"name": "API"}, {"name": "Auth"},
            ]},
        ])
        out = normalize_issue(issue)
        assert out["custom_fields"]["Subsystems"] == ["API", "Auth"]

    def test_custom_fields_handles_null_value(self):
        issue = _yt_issue(customFields=[
            {"name": "Optional", "value": None},
        ])
        out = normalize_issue(issue)
        assert out["custom_fields"]["Optional"] is None

    def test_unassigned_yields_none_login(self):
        issue = _yt_issue(assignee=None)
        out = normalize_issue(issue)
        assert out["assignee"] == "Unassigned"
        assert out["assignee_login"] is None

    def test_links_flattened(self):
        issue = _yt_issue(links=[{
            "direction": "outward",
            "linkType": {"name": "Depend"},
            "issues": [
                {"idReadable": "PROJ-99", "summary": "blocker", "state": {"name": "Open"}},
                {"idReadable": "PROJ-100", "summary": "other", "state": {"name": "Closed"}},
            ],
        }])
        out = normalize_issue(issue)
        assert len(out["links"]) == 2
        first = out["links"][0]
        assert first["id"] == "PROJ-99"
        assert first["link_type"] == "Depend"
        assert first["direction"] == "outward"
        assert first["state"] == "Open"


class TestNormalizeIssueComments:
    def test_comments_included_when_present(self):
        issue = _yt_issue()
        issue["comments"] = [
            {"id": "c1", "text": "first", "author": {"login": "bob.b", "name": "Bob B"},
             "created": 1747000000000},
        ]
        out = normalize_issue(issue, include_comments=True)
        assert len(out["comments"]) == 1
        assert out["comments"][0]["author"] == "Bob B"
        assert out["comments"][0]["author_login"] == "bob.b"

    def test_comments_omitted_when_absent_from_response(self):
        # When the YT response doesn't have a `comments` key at all,
        # we don't fabricate an empty list — keeps shape honest about
        # what was fetched.
        out = normalize_issue(_yt_issue())
        assert "comments" not in out

    def test_comments_skipped_when_include_comments_false(self):
        issue = _yt_issue()
        issue["comments"] = [{"id": "c1", "text": "x", "author": {"name": "x"}}]
        out = normalize_issue(issue, include_comments=False)
        assert "comments" not in out


class TestNormalizeIssueJSONRoundtrip:
    """Real consumer flow: normalize → json.dumps → json.loads → walk dict."""

    def test_roundtrip_preserves_all_keys(self):
        out = normalize_issue(_yt_issue())
        s = json.dumps(out, indent=2, ensure_ascii=False)
        parsed = json.loads(s)
        for key in ("id", "summary", "description", "state", "priority",
                    "assignee", "assignee_login", "created", "updated",
                    "resolved", "tags", "custom_fields", "links"):
            assert key in parsed

    def test_unicode_preserved_in_custom_field_names(self):
        out = normalize_issue(_yt_issue())
        s = json.dumps(out, indent=2, ensure_ascii=False)
        assert "☠️" in s
