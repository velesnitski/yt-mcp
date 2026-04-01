import os
import re

_ISSUE_URL_RE = re.compile(r"/issue/([A-Za-z]+-\d+)")

# Compact mode: strips markdown formatting for token savings (~60%)
# Set YOUTRACK_COMPACT=1 to enable
COMPACT = os.environ.get("YOUTRACK_COMPACT", "").lower() in ("1", "true", "yes")


def compact_lines(lines: list[str]) -> str:
    """Join lines, stripping markdown if compact mode is on."""
    text = "\n".join(lines)
    if not COMPACT:
        return text
    # Strip markdown formatting
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"^#+\s+", "", text, flags=re.MULTILINE)  # ## Headers → plain
    text = re.sub(r"^- ", "", text, flags=re.MULTILINE)  # bullet points
    text = re.sub(r"_([^_]+)_", r"\1", text)  # _italic_
    text = re.sub(r"\n{3,}", "\n\n", text)  # collapse blank lines
    return text.strip()


def parse_issue_id(issue_id: str) -> str:
    """Extract issue ID from a string that may be an ID or a YouTrack URL.

    Accepts:
        - 'PROJ-123'
        - 'https://company.youtrack.cloud/issue/PROJ-123/some-slug'
    """
    match = _ISSUE_URL_RE.search(issue_id)
    if match:
        return match.group(1)
    return issue_id.strip()


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
        if COMPACT:
            prod_str = f"|{product}" if product else ""
            lines.append(
                f"{issue.get('idReadable', '?')}|{state_name}{prod_str}|"
                f"{issue.get('summary', '')}|{assignee_name}"
            )
        else:
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
    product = get_product(data)
    tags = data.get("tags", [])
    desc = data.get("description")
    links = data.get("links", [])

    if COMPACT:
        iid = data.get("idReadable", "?")
        parts = [f"{iid}|{state_name}|{priority_name}|{assignee_name}"]
        if product:
            parts[0] += f"|{product}"
        parts.append(data.get("summary", ""))
        if tags:
            parts.append(f"Tags:{','.join(t.get('name', '') for t in tags)}")
        if desc:
            # Truncate description in compact mode
            parts.append(desc[:500] if len(desc) > 500 else desc)
        for link in links:
            lt = link.get("linkType", {}).get("name", "?")
            d = link.get("direction", "?")
            for linked in link.get("issues", []):
                ls = linked.get("state")
                st = ls.get("name", "") if isinstance(ls, dict) else ""
                parts.append(f"{lt}({d}):{linked.get('idReadable', '?')}[{st}]")
        comments = data.get("comments", [])
        if comments:
            for c in comments:
                author = c.get("author", {}).get("name", "?")
                text = c.get("text", "")[:200]
                parts.append(f"@{author}:{text}")
        return "\n".join(parts)

    parts = [
        f"# {data.get('idReadable', '?')}: {data.get('summary', '')}",
        "",
        f"**State:** {state_name}",
        f"**Priority:** {priority_name}",
        f"**Assignee:** {assignee_name}",
    ]

    if product:
        parts.append(f"**Product:** {product}")

    if tags:
        parts.append(f"**Tags:** {', '.join(t.get('name', '') for t in tags)}")

    if desc:
        parts.extend(["", "## Description", desc])

    if links:
        has_linked = False
        link_lines = []
        for link in links:
            link_type = link.get("linkType", {}).get("name", "?")
            direction = link.get("direction", "?")
            for linked in link.get("issues", []):
                linked_state = ""
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


# --- Shared constants for dashboard/monitoring tools ---

ISSUE_FIELDS = (
    "idReadable,summary,updated,created,state(name),priority(name),"
    "assignee(name),tags(name),"
    "customFields(name,value(name)),"
    "links(direction,linkType(name),issues(idReadable))"
)

ACTIVE_STATES = frozenset({"in progress", "submitted", "in review", "ready for test", "pause"})


def compile_exclude_patterns(exclude_patterns: str) -> list[re.Pattern]:
    """Compile comma-separated regex patterns for issue exclusion."""
    if not exclude_patterns:
        return []
    return [
        re.compile(p.strip(), re.IGNORECASE)
        for p in exclude_patterns.split(",")
        if p.strip()
    ]


def should_exclude(issue: dict, patterns: list[re.Pattern]) -> bool:
    """Check if issue summary matches any exclusion pattern."""
    summary = issue.get("summary", "")
    return any(p.search(summary) for p in patterns)


def format_value(val) -> str:
    if val is None:
        return "(empty)"
    if isinstance(val, list):
        names = [v.get("name", "") or v.get("text", "") for v in val]
        return ", ".join(names) if names else "(empty)"
    if isinstance(val, str):
        return val[:200] if len(val) > 200 else val
    return str(val)
