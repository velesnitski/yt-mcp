from yt_mcp.config import YouTrackConfig
from yt_mcp.tools import issues, templates, history, bulk, projects, translate, impact, users, articles

# Tools that modify data — blocked in read-only mode
WRITE_TOOLS = frozenset({
    "create_issue",
    "create_issue_from_template",
    "update_issue",
    "delete_issue",
    "add_comment",
    "update_comment",
    "delete_comment",
    "add_issue_link",
    "remove_issue_link",
    "add_work_item",
    "update_work_item",
    "delete_work_item",
    "bulk_update_execute",
    "bulk_rollback",
    "create_agile_board",
    "delete_agile_board",
    "apply_translations",
    "rollback_issue",
    "create_article",
    "update_article",
    "delete_article",
    "add_article_comment",
    "update_article_comment",
    "delete_article_comment",
})


def register_all(mcp, client, config: YouTrackConfig | None = None):
    # Collect all tools first, then filter
    modules = [issues, templates, history, bulk, projects, translate, impact, users, articles]
    for module in modules:
        module.register(mcp, client)

    if config is None:
        return

    # Build set of tools to remove
    to_remove = set()

    # Read-only mode: block all write tools
    if config.read_only:
        to_remove.update(WRITE_TOOLS)

    # Disabled tools from env
    if config.disabled_tools:
        to_remove.update(config.disabled_tools)

    # Remove blocked tools from the MCP server
    if to_remove and hasattr(mcp, "_tool_manager"):
        manager = mcp._tool_manager
        if hasattr(manager, "_tools"):
            for tool_name in to_remove:
                manager._tools.pop(tool_name, None)
