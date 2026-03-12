def _get_custom_field(issue: dict, field_name: str) -> str | None:
    """Extract a custom field value by name from an issue's customFields array.

    Handles SingleEnum (dict with 'name'), MultiEnum/User list (list of dicts),
    and plain string values. Returns the display string or None if not found.
    """
    for cf in issue.get("customFields", []):
        if cf.get("name") == field_name:
            val = cf.get("value")
            if val is None:
                return None
            if isinstance(val, dict):
                return val.get("name")
            if isinstance(val, list):
                names = [v.get("name", "") for v in val if isinstance(v, dict) and v.get("name")]
                return ", ".join(names) if names else None
            if isinstance(val, str):
                return val
    return None


def get_product(issue: dict) -> str:
    """Extract Product field value from an issue's custom fields."""
    return _get_custom_field(issue, "Product") or ""


def _resolve_state(issue: dict) -> str:
    """Get state name from top-level field or customFields fallback."""
    state = issue.get("state")
    if state and isinstance(state, dict):
        name = state.get("name")
        if name:
            return name
    return _get_custom_field(issue, "State") or "Unknown"


def _resolve_priority(issue: dict) -> str:
    """Get priority name from top-level field or customFields fallback."""
    priority = issue.get("priority")
    if priority and isinstance(priority, dict):
        name = priority.get("name")
        if name:
            return name
    return _get_custom_field(issue, "Priority") or "?"


def _resolve_assignee(issue: dict) -> str:
    """Get assignee name from top-level field or customFields fallback."""
    assignee = issue.get("assignee")
    if assignee and isinstance(assignee, dict):
        name = assignee.get("name")
        if name:
            return name
    cf_assignee = _get_custom_field(issue, "Assignee")
    return cf_assignee or "Unassigned"


def format_issue_list(issues: list) -> str:
    if not issues:
        return "No issues found."
    lines = []
    for issue in issues:
        assignee_name = _resolve_assignee(issue)
        state_name = _resolve_state(issue)
        product = get_product(issue)
        product_str = f" ({product})" if product else ""
        lines.append(
            f"- **{issue.get('idReadable', '?')}** [{state_name}]{product_str} "
            f"{issue.get('summary', 'No summary')} → {assignee_name}"
        )
    return "\n".join(lines)


def format_issue_detail(data: dict) -> str:
    state_name = _resolve_state(data)
    priority_name = _resolve_priority(data)
    assignee_name = _resolve_assignee(data)

    parts = [
        f"# {data.get('idReadable', '?')}: {data.get('summary', '')}",
        "",
        f"**State:** {state_name}",
        f"**Priority:** {priority_name}",
        f"**Assignee:** {assignee_name}",
    ]

    product = get_product(data)
    if product:
        parts.append(f"**Product:** {product}")

    tags = data.get("tags", [])
    if tags:
        parts.append(f"**Tags:** {', '.join(t.get('name', '') for t in tags)}")

    desc = data.get("description")
    if desc:
        parts.extend(["", "## Description", desc])

    # Links
    links = data.get("links", [])
    if links:
        has_linked = False
        link_lines = []
        for link in links:
            link_type = link.get("linkType", {}).get("name", "?")
            direction = link.get("direction", "?")
            for linked in link.get("issues", []):
                linked_state = ""
                # Try top-level state first, then customFields
                ls = linked.get("state")
                if ls and isinstance(ls, dict) and ls.get("name"):
                    linked_state = ls["name"]
                else:
                    linked_state = _get_custom_field(linked, "State") or ""
                state_str = f" [{linked_state}]" if linked_state else ""
                link_lines.append(
                    f"- **{link_type}** ({direction}): "
                    f"{linked.get('idReadable', '?')}{state_str} — {linked.get('summary', '')}"
                )
                has_linked = True
        if has_linked:
            parts.append("")
            parts.append("## Links")
            parts.extend(link_lines)

    comments = data.get("comments", [])
    if comments:
        parts.extend(["", f"## Comments ({len(comments)})"])
        for c in comments:
            author = c.get("author", {}).get("name", "Unknown")
            parts.append(f"**{author}:** {c.get('text', '')}")
            parts.append("")

    return "\n".join(parts)


def format_value(val) -> str:
    if val is None:
        return "(empty)"
    if isinstance(val, list):
        names = [v.get("name", "") or v.get("text", "") for v in val]
        return ", ".join(names) if names else "(empty)"
    if isinstance(val, str):
        return val[:200] if len(val) > 200 else val
    return str(val)
