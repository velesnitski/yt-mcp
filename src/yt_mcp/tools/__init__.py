from yt_mcp.config import YouTrackConfig
from yt_mcp.resolver import InstanceResolver
from yt_mcp.logging import logged
from yt_mcp.tools import issues, comments, attachments, templates, history, bulk, projects, sprints, discovery, translate, impact, users, articles, dashboard, monitoring, journey, deadlines, pulse, handoffs, time_report, releases

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

# The "core" toolset (YOUTRACK_TOOLSET=core): the everyday issue-CRUD surface,
# deliberately sized like the official YouTrack MCP server's ~23 predefined
# tools. Exists for token economics — all 81 schemas cost ~21K context tokens
# per session on clients WITHOUT deferred tool loading (Cursor, n8n, …);
# core cuts that ~4x. Analytics/reporting/bulk tools need "full". ADR-026.
CORE_TOOLS = frozenset({
    # issues
    "search_issues", "get_issue", "get_issues", "count_issues",
    "create_issue", "update_issue", "transition_issue",
    "add_comment",
    "get_issue_links", "add_issue_link",
    # projects & discovery
    "list_projects", "get_project_fields",
    "list_tags", "list_saved_searches", "run_saved_search",
    # people & instance
    "get_current_user", "search_users", "get_instance_url",
    # knowledge base (read side)
    "search_articles", "get_article",
})


# --- MCP tool annotations (ADR-045) -----------------------------------------
# The spec's four hints let a client reason about a tool before calling it:
# gate writes behind confirmation, parallelize reads, retry safely. They are
# advertised in tools/list and cost nothing at runtime.
#
# readOnlyHint is DERIVED from WRITE_TOOLS above rather than restated, so a
# new write tool cannot be annotated read-only by omission — the existing set
# is already the thing every write path must be registered in.

# Write tools that remove or revert data. Per spec, destructiveHint is only
# meaningful for non-read-only tools; additive writes get False.
DESTRUCTIVE_TOOLS = frozenset({
    "delete_issue", "delete_comment", "delete_work_item", "delete_article",
    "delete_article_comment", "delete_agile_board",
    "rollback_issue", "bulk_rollback", "remove_issue_link",
    "bulk_update_execute",  # mass field overwrite; rollback exists but it is a bulk mutation
})

# Writes that create a NEW entity each call — calling twice is not the same as
# calling once. Everything else (setting a field to a value, deleting a thing
# already gone) converges on the same state.
NON_IDEMPOTENT_TOOLS = frozenset({
    "create_issue", "create_issue_from_template", "create_article",
    "create_agile_board", "create_sprint",
    "add_comment", "add_article_comment", "add_work_item", "add_attachment",
})


def _annotate_tools(tools: dict) -> None:
    """Backstop for any tool registered without explicit annotations.

    Tools declare their hints at the definition site (`annotations=` on the
    decorator, ADR-046) so a reader — or a registry auditing the source —
    sees them without running the server. This pass only fills a tool that
    declared none, so a future omission degrades to a correct default
    instead of shipping a tool with no hints. Tests assert the two agree.
    """
    from mcp.types import ToolAnnotations

    for name, tool in tools.items():
        if tool.annotations is not None:
            continue
        read_only = name not in WRITE_TOOLS
        tool.annotations = ToolAnnotations(
            readOnlyHint=read_only,
            destructiveHint=False if read_only else name in DESTRUCTIVE_TOOLS,
            idempotentHint=True if read_only else name not in NON_IDEMPOTENT_TOOLS,
            # Every tool talks to a YouTrack instance — an external system
            # whose contents change outside this server.
            openWorldHint=True,
        )


def _registered_tools(mcp) -> dict:
    """The ONE place that touches FastMCP's private tool registry.

    FastMCP has no public API to enumerate/mutate registered tools after the
    fact, so we reach into `_tool_manager._tools` — a version-coupled hack
    (pin: mcp>=1.28.1,<2.0 in pyproject). Keeping every reach-in behind this
    accessor means an SDK layout change breaks exactly one function, and the
    hasattr guards degrade to a no-op ({}), never a crash.
    """
    manager = getattr(mcp, "_tool_manager", None)
    return getattr(manager, "_tools", None) or {}


def register_all(mcp, resolver: InstanceResolver, config: YouTrackConfig | None = None):
    # Collect all tools first, then filter
    modules = [issues, comments, attachments, templates, history, bulk, projects, sprints, discovery, translate, impact, users, articles, dashboard, monitoring, journey, deadlines, pulse, handoffs, time_report, releases]
    for module in modules:
        module.register(mcp, resolver)

    tools = _registered_tools(mcp)
    _annotate_tools(tools)

    # Wrap all tool functions with analytics logging
    for tool in tools.values():
        if hasattr(tool, "fn"):
            tool.fn = logged(tool.fn)

    if config is None:
        return

    # Build set of tools to remove
    to_remove = set()

    # Core toolset: keep only the everyday issue-CRUD surface (token economics)
    if getattr(config, "toolset", "full") == "core":
        to_remove.update(set(tools) - CORE_TOOLS)

    # Read-only mode: block all write tools
    if config.read_only:
        to_remove.update(WRITE_TOOLS)

    # Disabled tools from env
    if config.disabled_tools:
        to_remove.update(config.disabled_tools)

    for tool_name in to_remove:
        tools.pop(tool_name, None)
