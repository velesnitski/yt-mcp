import os
from unittest.mock import patch, AsyncMock, MagicMock

from mcp.server.fastmcp import FastMCP

from yt_mcp.config import YouTrackConfig
from yt_mcp.resolver import InstanceResolver
from yt_mcp.tools import register_all, WRITE_TOOLS


def _make_client():
    """Create a mock YouTrackClient."""
    cfg = YouTrackConfig(url="https://test.youtrack.cloud", token="perm:test")
    mock = MagicMock()
    mock._config = cfg
    mock.get = AsyncMock(return_value=[])
    mock.post = AsyncMock(return_value={})
    mock.delete = AsyncMock()
    mock.execute_command = AsyncMock()
    mock.update_comment = AsyncMock(return_value={})
    mock.resolve_project_id = AsyncMock(return_value="test-id")
    return mock


def _make_resolver(clients=None):
    """Create a resolver with mock client(s)."""
    if clients is None:
        clients = {"default": _make_client()}
    return InstanceResolver(clients)


def _get_tool_names(mcp: FastMCP) -> set[str]:
    """Extract registered tool names from MCP server."""
    if hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools"):
        return set(mcp._tool_manager._tools.keys())
    return set()


class TestToolRegistration:
    def test_all_tools_registered(self):
        mcp = FastMCP("test")
        resolver = _make_resolver()
        config = YouTrackConfig(url="https://test.youtrack.cloud", token="perm:test")
        register_all(mcp, resolver, config)

        tools = _get_tool_names(mcp)
        assert len(tools) == 53, f"Expected 43 tools, got {len(tools)}: {sorted(tools)}"

    def test_expected_tools_present(self):
        mcp = FastMCP("test")
        resolver = _make_resolver()
        config = YouTrackConfig(url="https://test.youtrack.cloud", token="perm:test")
        register_all(mcp, resolver, config)

        tools = _get_tool_names(mcp)
        expected = {
            "search_issues", "get_issue", "create_issue", "update_issue",
            "delete_issue", "get_issue_links", "add_issue_link", "remove_issue_link",
            "add_comment", "update_comment", "delete_comment", "poll_changes",
            "get_top_active_issues", "get_top_blocked_issues", "get_team_dashboard", "get_multi_team_dashboard",
            "get_issues_digest", "get_at_risk_issues",
            "check_task_creation", "get_creation_activity", "get_project_health",
            "list_projects", "get_agiles", "get_agile_board", "create_agile_board",
            "delete_agile_board", "get_sprint_board",
            "list_templates", "create_issue_from_template",
            "get_issue_history", "rollback_issue", "get_work_items",
            "add_work_item", "update_work_item", "delete_work_item",
            "get_issue_changes_summary",
            "bulk_update_preview", "bulk_update_execute", "bulk_rollback",
            "get_issues_for_translation", "apply_translations",
            "get_impact_map", "get_deadline_impact",
            "get_current_user", "search_users",
            "search_articles", "get_article", "create_article",
            "update_article", "delete_article",
            "add_article_comment", "update_article_comment", "delete_article_comment",
        }
        missing = expected - tools
        extra = tools - expected
        assert not missing, f"Missing tools: {missing}"
        assert not extra, f"Unexpected tools: {extra}"

    def test_read_only_removes_write_tools(self):
        mcp = FastMCP("test")
        resolver = _make_resolver()
        config = YouTrackConfig(
            url="https://test.youtrack.cloud",
            token="perm:test",
            read_only=True,
        )
        register_all(mcp, resolver, config)

        tools = _get_tool_names(mcp)
        for wt in WRITE_TOOLS:
            assert wt not in tools, f"Write tool '{wt}' should be blocked in read-only mode"

    def test_read_only_keeps_read_tools(self):
        mcp = FastMCP("test")
        resolver = _make_resolver()
        config = YouTrackConfig(
            url="https://test.youtrack.cloud",
            token="perm:test",
            read_only=True,
        )
        register_all(mcp, resolver, config)

        tools = _get_tool_names(mcp)
        read_tools = {"search_issues", "get_issue", "list_projects", "get_agiles",
                       "get_issue_history", "get_work_items", "search_users",
                       "get_current_user", "search_articles", "get_article"}
        for rt in read_tools:
            assert rt in tools, f"Read tool '{rt}' should remain in read-only mode"

    def test_disabled_tools_removed(self):
        mcp = FastMCP("test")
        resolver = _make_resolver()
        config = YouTrackConfig(
            url="https://test.youtrack.cloud",
            token="perm:test",
            disabled_tools=frozenset({"delete_issue", "bulk_update_execute"}),
        )
        register_all(mcp, resolver, config)

        tools = _get_tool_names(mcp)
        assert "delete_issue" not in tools
        assert "bulk_update_execute" not in tools
        assert "search_issues" in tools  # not disabled

    def test_disabled_tools_can_remove_read_tools(self):
        mcp = FastMCP("test")
        resolver = _make_resolver()
        config = YouTrackConfig(
            url="https://test.youtrack.cloud",
            token="perm:test",
            disabled_tools=frozenset({"search_issues", "get_impact_map"}),
        )
        register_all(mcp, resolver, config)

        tools = _get_tool_names(mcp)
        assert "search_issues" not in tools
        assert "get_impact_map" not in tools

    def test_write_tools_set_complete(self):
        """Verify WRITE_TOOLS contains all tools that modify data."""
        expected_write = {
            "create_issue", "create_issue_from_template", "update_issue",
            "delete_issue", "add_comment", "update_comment", "delete_comment",
            "add_issue_link", "remove_issue_link",
            "add_work_item", "update_work_item", "delete_work_item",
            "bulk_update_execute", "bulk_rollback",
            "create_agile_board", "delete_agile_board",
            "apply_translations", "rollback_issue",
            "create_article", "update_article", "delete_article",
            "add_article_comment", "update_article_comment", "delete_article_comment",
        }
        assert WRITE_TOOLS == expected_write, (
            f"Missing from WRITE_TOOLS: {expected_write - WRITE_TOOLS}\n"
            f"Extra in WRITE_TOOLS: {WRITE_TOOLS - expected_write}"
        )

    def test_no_config_registers_all(self):
        mcp = FastMCP("test")
        resolver = _make_resolver()
        register_all(mcp, resolver, config=None)

        tools = _get_tool_names(mcp)
        assert len(tools) == 53
