from yt_mcp.config import YouTrackConfig
from yt_mcp.resolver import InstanceResolver
from yt_mcp.logging import logged
from yt_mcp.tools import issues, comments, attachments, templates, history, bulk, projects, sprints, discovery, translate, impact, users, articles, dashboard, monitoring, journey, deadlines, pulse, handoffs

# Tools that modify data — blocked in read-only mode
WRITE_TOOLS = frozenset({
    "create_issue",
    "create_issue_from_template",
    "update_issue",
    "transition_issue",
    "delete_issue",
    "add_comment",
    "update_comment",
    "delete_comment",
    "add_attachment",
    "add_issue_link",
    "remove_issue_link",
    "add_work_item",
    "update_work_item",
    "delete_work_item",
    "bulk_update_execute",
    "bulk_rollback",
    "create_agile_board",
    "delete_agile_board",
    "create_sprint",
    "update_sprint",
    "add_issues_to_sprint",
    "apply_translations",
    "rollback_issue",
    "create_article",
    "update_article",
    "delete_article",
    "add_article_comment",
    "update_article_comment",
    "delete_article_comment",
})


def _registered_tools(mcp) -> dict:
    """The ONE place that touches FastMCP's private tool registry.

    FastMCP has no public API to enumerate/mutate registered tools after the
    fact, so we reach into `_tool_manager._tools` — a version-coupled hack
    (pin: mcp>=1.0,<1.26 in pyproject). Keeping every reach-in behind this
    accessor means an SDK layout change breaks exactly one function, and the
    hasattr guards degrade to a no-op ({}), never a crash.
    """
    manager = getattr(mcp, "_tool_manager", None)
    return getattr(manager, "_tools", None) or {}


def register_all(mcp, resolver: InstanceResolver, config: YouTrackConfig | None = None):
    # Collect all tools first, then filter
    modules = [issues, comments, attachments, templates, history, bulk, projects, sprints, discovery, translate, impact, users, articles, dashboard, monitoring, journey, deadlines, pulse, handoffs]
    for module in modules:
        module.register(mcp, resolver)

    tools = _registered_tools(mcp)

    # Wrap all tool functions with analytics logging
    for tool in tools.values():
        if hasattr(tool, "fn"):
            tool.fn = logged(tool.fn)

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

    for tool_name in to_remove:
        tools.pop(tool_name, None)
