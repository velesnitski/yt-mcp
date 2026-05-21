import os
import re

_ISSUE_URL_RE = re.compile(r"/issue/([A-Za-z]+-\d+)")

# Compact mode: strips markdown formatting for token savings (~60%)
# Set YOUTRACK_COMPACT=1 to enable
COMPACT = os.environ.get("YOUTRACK_COMPACT", "").lower() in ("1", "true", "yes")


def escape_query_value(value: str) -> str:
    """Escape a value for safe use in YouTrack search queries."""
    # Remove braces and backslashes that could break query syntax
    return value.replace("\\", "").replace("{", "").replace("}", "")


def build_state_clause(states: list[str]) -> str:
    """Build a `State:` clause in YT's comma-list idiom.

    YouTrack accepts `State: {A}, {B}, {C}` (comma-list) but rejects
    `(State: {A} or State: {B})` (OR-joined repeated-field) on many
    versions/projects — surfaces as 400 "Can't parse search query".

    Empty input returns empty string; the caller can compose conditionally.
    """
    if not states:
        return ""
    return "State: " + ", ".join(f"{{{s}}}" for s in states)


def normalize_issue(data: dict, include_comments: bool = True) -> dict:
    """Flatten a YT issue response into a JSON-friendly dict.

    YT's native shape has `customFields` as a list of `{name, value}` pairs
    and tags/links as nested objects — readable, but every consumer has to
    walk the same boilerplate. This helper produces a clean dict that
    matches the shape used by other tools' `format="json"` outputs (pulse,
    handoffs):

      {
        "id", "summary", "description",
        "state", "priority",
        "assignee", "assignee_login",
        "created", "updated", "resolved",
        "tags": [str, ...],
        "custom_fields": {field_name: value, ...},
        "links": [{id, summary, state, link_type, direction}, ...],
        "comments": [{id, text, author, author_login, created}, ...],
      }
    """
    custom_fields: dict[str, object] = {}
    for cf in data.get("customFields", []) or []:
        name = cf.get("name")
        if not name:
            continue
        val = cf.get("value")
        if isinstance(val, dict):
            custom_fields[name] = (
                val.get("name") or val.get("presentation")
                or val.get("text") or val.get("login")
            )
        elif isinstance(val, list):
            custom_fields[name] = [
                v.get("name") or v.get("presentation") or v.get("text") or v.get("login")
                for v in val if isinstance(v, dict)
            ]
        else:
            custom_fields[name] = val

    tags = [t.get("name", "") for t in (data.get("tags") or []) if t.get("name")]

    out: dict = {
        "id": data.get("idReadable", ""),
        "summary": data.get("summary", "") or "",
        "description": data.get("description") or "",
        "state": _resolve_state(data),
        "priority": _resolve_priority(data),
        "assignee": _resolve_assignee(data),
        "assignee_login": _resolve_assignee_login(data),
        "created": data.get("created"),
        "updated": data.get("updated"),
        "resolved": data.get("resolved"),
        "tags": tags,
        "custom_fields": custom_fields,
    }

    links: list[dict] = []
    for link in data.get("links", []) or []:
        link_type = (link.get("linkType") or {}).get("name", "")
        direction = link.get("direction", "")
        for linked in link.get("issues", []) or []:
            ls = linked.get("state")
            state = ls.get("name", "") if isinstance(ls, dict) else ""
            links.append({
                "id": linked.get("idReadable", ""),
                "summary": linked.get("summary", "") or "",
                "state": state,
                "link_type": link_type,
                "direction": direction,
            })
    out["links"] = links

    if include_comments and data.get("comments") is not None:
        out["comments"] = [
            {
                "id": c.get("id", ""),
                "text": c.get("text", "") or "",
                "author": (c.get("author") or {}).get("name", ""),
                "author_login": (c.get("author") or {}).get("login"),
                "created": c.get("created"),
            }
            for c in (data.get("comments") or [])
        ]
    return out


def build_absolute_date_clause(days_ago: int, now_ms: int) -> str:
    """Absolute-date range `YYYY-MM-DD .. YYYY-MM-DD` for `updated:` /
    `created:` / `resolved:` filters. Relative bounds like `-30d` or
    `{minus 14d}` are version-dependent in YT — absolute dates work
    everywhere."""
    from datetime import datetime, timedelta, timezone
    end_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    start_dt = end_dt - timedelta(days=days_ago)
    return f"{start_dt.strftime('%Y-%m-%d')} .. {end_dt.strftime('%Y-%m-%d')}"


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


def _resolve_assignee_login(issue: dict) -> str | None:
    """Get assignee login (YouTrack username) for live `Assignee:` query
    filters. YouTrack's `Assignee:` syntax requires the login string, NOT
    the display name. Returns None when no login is resolvable so callers
    can fall back to ID-list queries."""
    a = issue.get("assignee")
    if isinstance(a, dict) and a.get("login"):
        return a["login"]
    for cf in issue.get("customFields", []):
        if cf.get("name") == "Assignee":
            v = cf.get("value")
            if isinstance(v, dict) and v.get("login"):
                return v["login"]
            if isinstance(v, list) and v:
                first = v[0]
                if isinstance(first, dict) and first.get("login"):
                    return first["login"]
    return None


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

ACTIVE_STATES = frozenset({"in progress", "submitted", "in review", "ready for test"})


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
