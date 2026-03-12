def get_product(issue: dict) -> str:
    """Extract Product field value from an issue's custom fields."""
    for cf in issue.get("customFields", []):
        if cf.get("name") == "Product":
            val = cf.get("value")
            if isinstance(val, list):
                return ", ".join(v.get("name", "") for v in val if v.get("name"))
            if isinstance(val, dict) and val.get("name"):
                return val["name"]
    return ""


def format_issue_list(issues: list) -> str:
    if not issues:
        return "No issues found."
    lines = []
    for issue in issues:
        assignee = issue.get("assignee", {})
        assignee_name = assignee.get("name", "Unassigned") if assignee else "Unassigned"
        state = issue.get("state", {})
        state_name = state.get("name", "Unknown") if state else "Unknown"
        product = get_product(issue)
        product_str = f" ({product})" if product else ""
        lines.append(
            f"- **{issue.get('idReadable', '?')}** [{state_name}]{product_str} "
            f"{issue.get('summary', 'No summary')} → {assignee_name}"
        )
    return "\n".join(lines)


def format_issue_detail(data: dict) -> str:
    parts = [
        f"# {data.get('idReadable', '?')}: {data.get('summary', '')}",
        "",
        f"**State:** {data.get('state', {}).get('name', '?')}",
        f"**Priority:** {data.get('priority', {}).get('name', '?')}",
    ]

    assignee = data.get("assignee")
    parts.append(f"**Assignee:** {assignee.get('name') if assignee else 'Unassigned'}")

    product = get_product(data)
    if product:
        parts.append(f"**Product:** {product}")

    tags = data.get("tags", [])
    if tags:
        parts.append(f"**Tags:** {', '.join(t.get('name', '') for t in tags)}")

    desc = data.get("description")
    if desc:
        parts.extend(["", "## Description", desc])

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
