import os
import json
from mcp.server.fastmcp import FastMCP
import httpx

mcp = FastMCP("youtrack")

YOUTRACK_URL = os.environ.get("YOUTRACK_URL", "").rstrip("/")
YOUTRACK_TOKEN = os.environ.get("YOUTRACK_TOKEN", "")


def _headers():
    return {
        "Authorization": f"Bearer {YOUTRACK_TOKEN}",
        "Accept": "application/json",
    }


def _get_product(issue: dict) -> str:
    """Extract Product field value from an issue's custom fields."""
    for cf in issue.get("customFields", []):
        if cf.get("name") == "Product":
            val = cf.get("value")
            if isinstance(val, list):
                return ", ".join(v.get("name", "") for v in val if v.get("name"))
            if isinstance(val, dict) and val.get("name"):
                return val["name"]
    return ""


def _format_issues(issues: list) -> str:
    if not issues:
        return "No issues found."
    lines = []
    for issue in issues:
        assignee = issue.get("assignee", {})
        assignee_name = assignee.get("name", "Unassigned") if assignee else "Unassigned"
        state = issue.get("state", {})
        state_name = state.get("name", "Unknown") if state else "Unknown"
        product = _get_product(issue)
        product_str = f" ({product})" if product else ""
        lines.append(
            f"- **{issue.get('idReadable', '?')}** [{state_name}]{product_str} "
            f"{issue.get('summary', 'No summary')} → {assignee_name}"
        )
    return "\n".join(lines)


@mcp.tool()
async def search_issues(query: str, max_results: int = 50) -> str:
    """Search YouTrack issues using YouTrack query syntax.

    Examples:
        - "project: Android state: Open"
        - "project: DevOps updated: -1w"
        - "assignee: me tag: urgent"
        - "#Unresolved project: WordPress"
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{YOUTRACK_URL}/api/issues",
            params={
                "query": query,
                "fields": "idReadable,summary,state(name),assignee(name),customFields(name,value(name)),created,updated",
                "$top": str(max_results),
            },
            headers=_headers(),
        )
        resp.raise_for_status()
        return _format_issues(resp.json())


@mcp.tool()
async def get_issue(issue_id: str) -> str:
    """Get full details of a specific YouTrack issue by its ID (e.g., 'DEVOPS-423')."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{YOUTRACK_URL}/api/issues/{issue_id}",
            params={
                "fields": "idReadable,summary,description,state(name),priority(name),"
                "assignee(name),created,updated,resolved,"
                "comments(text,author(name),created),"
                "tags(name),customFields(name,value(name))",
            },
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

        parts = [
            f"# {data.get('idReadable', '?')}: {data.get('summary', '')}",
            "",
            f"**State:** {data.get('state', {}).get('name', '?')}",
            f"**Priority:** {data.get('priority', {}).get('name', '?')}",
        ]

        assignee = data.get("assignee")
        parts.append(f"**Assignee:** {assignee.get('name') if assignee else 'Unassigned'}")

        product = _get_product(data)
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


@mcp.tool()
async def list_projects() -> str:
    """List all accessible YouTrack projects."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{YOUTRACK_URL}/api/admin/projects",
            params={"fields": "shortName,name,archived,leader(name)"},
            headers=_headers(),
        )
        resp.raise_for_status()
        projects = resp.json()
        lines = []
        for p in projects:
            status = " (archived)" if p.get("archived") else ""
            leader = p.get("leader", {})
            leader_name = leader.get("name", "?") if leader else "?"
            lines.append(f"- **{p.get('shortName', '?')}** — {p.get('name', '?')}{status} (lead: {leader_name})")
        return "\n".join(lines) if lines else "No projects found."


@mcp.tool()
async def get_agiles() -> str:
    """List all agile boards in YouTrack."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{YOUTRACK_URL}/api/agiles",
            params={"fields": "id,name,projects(shortName,name),owner(name)"},
            headers=_headers(),
        )
        resp.raise_for_status()
        boards = resp.json()
        lines = []
        for b in boards:
            projects = ", ".join(p.get("shortName", "?") for p in b.get("projects", []))
            owner = b.get("owner", {})
            owner_name = owner.get("name", "?") if owner else "?"
            lines.append(f"- **{b.get('name', '?')}** (projects: {projects}, owner: {owner_name})")
        return "\n".join(lines) if lines else "No agile boards found."


async def _set_product(client: httpx.AsyncClient, issue_id: str, product: str) -> None:
    """Set the Product custom field on an issue via command."""
    resp = await client.post(
        f"{YOUTRACK_URL}/api/issues/{issue_id}/execute",
        json={"query": f"Product {product}"},
        headers={**_headers(), "Content-Type": "application/json"},
    )
    resp.raise_for_status()


ISSUE_TEMPLATES = {
    "bug": {
        "name": "Bug Report",
        "sections": [
            ("Summary", "Brief description of the bug"),
            ("Steps to Reproduce", "1. \n2. \n3. "),
            ("Expected Result", "What should happen"),
            ("Actual Result", "What actually happens"),
            ("Environment", "OS, browser, app version, device"),
            ("Severity", "Critical / Major / Minor / Trivial"),
            ("Screenshots / Logs", "Attach if available"),
        ],
    },
    "feature": {
        "name": "Feature Request",
        "sections": [
            ("Problem", "What problem does this solve?"),
            ("Proposed Solution", "How should it work?"),
            ("Alternatives Considered", "Other approaches evaluated"),
            ("Acceptance Criteria", "- [ ] Criterion 1\n- [ ] Criterion 2"),
            ("Priority", "Must have / Should have / Nice to have"),
        ],
    },
    "task": {
        "name": "Task",
        "sections": [
            ("Objective", "What needs to be done"),
            ("Requirements", "- Requirement 1\n- Requirement 2"),
            ("Technical Notes", "Implementation details, links, references"),
            ("Definition of Done", "- [ ] Done criterion 1\n- [ ] Done criterion 2"),
        ],
    },
    "daily": {
        "name": "Daily Standup",
        "sections": [
            ("Done Yesterday", "- "),
            ("Planned Today", "- "),
            ("Blockers", "None"),
        ],
    },
    "spike": {
        "name": "Research / Spike",
        "sections": [
            ("Goal", "What are we trying to learn?"),
            ("Context", "Why is this research needed?"),
            ("Scope", "What's in/out of scope"),
            ("Timebox", "Maximum time to spend"),
            ("Findings", "To be filled after research"),
            ("Recommendation", "To be filled after research"),
        ],
    },
    "release": {
        "name": "Release Checklist",
        "sections": [
            ("Version", "x.y.z"),
            ("Changes Included", "- "),
            ("Pre-release Checklist", "- [ ] Tests pass\n- [ ] Code review done\n- [ ] Staging tested"),
            ("Post-release Checklist", "- [ ] Production verified\n- [ ] Monitoring checked\n- [ ] Docs updated"),
            ("Rollback Plan", "Steps to rollback if needed"),
        ],
    },
    "devops": {
        "name": "DevOps / Infrastructure Task",
        "sections": [
            ("Description", "Brief overview of the infrastructure task and its scope"),
            ("Requirement", "- What needs to be deployed, configured, or changed\n- Capacity and performance considerations\n- Monitoring and alerting requirements"),
            ("Expected Result", "- Servers/services are deployed and operational\n- Monitoring confirms healthy state\n- Documentation and scripts are updated"),
            ("Affected Services", "List of services, servers, or environments involved"),
            ("Rollback Plan", "Steps to revert changes if something goes wrong"),
        ],
    },
}


@mcp.tool()
async def list_templates() -> str:
    """List all available issue templates with their sections."""
    lines = ["## Available issue templates", ""]
    for key, tpl in ISSUE_TEMPLATES.items():
        sections = ", ".join(s[0] for s in tpl["sections"])
        lines.append(f"- **{key}** — {tpl['name']} ({sections})")
    lines.append("")
    lines.append("Use `create_issue_from_template` to create an issue with a template.")
    return "\n".join(lines)


@mcp.tool()
async def create_issue_from_template(
    project: str,
    template: str,
    summary: str,
    fields: str = "",
    product: str = "",
) -> str:
    """Create a YouTrack issue using a predefined template.

    The template provides the description structure. You can fill in sections
    by passing field values in the 'fields' parameter.

    Args:
        project: Project short name (e.g., 'DO', 'AP')
        template: Template name (bug, feature, task, daily, spike, release, devops)
        summary: Issue title
        fields: Section values as 'Section Name: value' separated by '|||'.
                Example: 'Steps to Reproduce: 1. Open app 2. Click X|||Expected Result: Page loads|||Severity: Major'
                Sections not provided will keep placeholder text.
        product: Product name for the Product custom field (leave empty to skip)
    """
    tpl = ISSUE_TEMPLATES.get(template.lower())
    if not tpl:
        available = ", ".join(ISSUE_TEMPLATES.keys())
        return f"Unknown template '{template}'. Available: {available}"

    # Parse provided field values
    provided = {}
    if fields:
        for pair in fields.split("|||"):
            pair = pair.strip()
            if ":" in pair:
                key, value = pair.split(":", 1)
                provided[key.strip().lower()] = value.strip()

    # Build description from template
    desc_parts = []
    for section_name, placeholder in tpl["sections"]:
        value = provided.get(section_name.lower(), placeholder)
        desc_parts.append(f"## {section_name}\n{value}")

    description = "\n\n".join(desc_parts)

    # Create the issue using the existing create_issue logic
    payload = {
        "project": {"id": None},
        "summary": summary,
        "description": description,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        proj_resp = await client.get(
            f"{YOUTRACK_URL}/api/admin/projects",
            params={"query": f"shortName: {project}", "fields": "id,shortName"},
            headers=_headers(),
        )
        proj_resp.raise_for_status()
        projects = proj_resp.json()
        if not projects:
            return f"Project '{project}' not found."
        payload["project"]["id"] = projects[0]["id"]

        resp = await client.post(
            f"{YOUTRACK_URL}/api/issues",
            json=payload,
            headers={**_headers(), "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        issue_id = data.get("idReadable", "?")

        product_str = ""
        if product:
            await _set_product(client, issue_id, product)
            product_str = f"\n**Product:** {product}"

        return (
            f"Created from **{tpl['name']}** template: "
            f"**{issue_id}** — {data.get('summary', '')}{product_str}\n\n"
            f"**Description preview:**\n{description}"
        )


@mcp.tool()
async def create_issue(project: str, summary: str, description: str = "", product: str = "") -> str:
    """Create a new issue in a YouTrack project.

    Args:
        project: Project short name (e.g., 'DO', 'AP')
        summary: Issue title
        description: Issue description (markdown supported)
        product: Product name for the Product custom field (leave empty to skip)
    """
    payload = {
        "project": {"id": None},
        "summary": summary,
        "description": description,
    }

    # First resolve project ID
    async with httpx.AsyncClient(timeout=30) as client:
        proj_resp = await client.get(
            f"{YOUTRACK_URL}/api/admin/projects",
            params={"query": f"shortName: {project}", "fields": "id,shortName"},
            headers=_headers(),
        )
        proj_resp.raise_for_status()
        projects = proj_resp.json()
        if not projects:
            return f"Project '{project}' not found."
        payload["project"]["id"] = projects[0]["id"]

        resp = await client.post(
            f"{YOUTRACK_URL}/api/issues",
            json=payload,
            headers={**_headers(), "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        issue_id = data.get("idReadable", "?")

        product_str = ""
        if product:
            await _set_product(client, issue_id, product)
            product_str = f" | **Product:** {product}"

        return f"Created: **{issue_id}** — {data.get('summary', '')}{product_str}"


@mcp.tool()
async def update_issue(
    issue_id: str,
    summary: str = "",
    description: str = "",
    state: str = "",
    assignee: str = "",
    product: str = "",
) -> str:
    """Update fields of an existing YouTrack issue.

    Args:
        issue_id: Issue ID (e.g., 'DEVOPS-423')
        summary: New title (leave empty to keep current)
        description: New description (leave empty to keep current)
        state: New state name (e.g., 'In Progress', 'Done', 'Open')
        assignee: New assignee login or full name (leave empty to keep current)
        product: Product name for the Product custom field (leave empty to keep current)
    """
    payload: dict = {}
    if summary:
        payload["summary"] = summary
    if description:
        payload["description"] = description

    if not payload and not state and not assignee and not product:
        return "Nothing to update — provide at least one field."

    async with httpx.AsyncClient(timeout=30) as client:
        if payload:
            resp = await client.post(
                f"{YOUTRACK_URL}/api/issues/{issue_id}",
                json=payload,
                headers={**_headers(), "Content-Type": "application/json"},
            )
            resp.raise_for_status()

        commands = []
        if state:
            commands.append(f"State {state}")
        if assignee:
            commands.append(f"Assignee {assignee}")
        if product:
            commands.append(f"Product {product}")

        if commands:
            resp = await client.post(
                f"{YOUTRACK_URL}/api/issues/{issue_id}/execute",
                json={"query": " ".join(commands)},
                headers={**_headers(), "Content-Type": "application/json"},
            )
            resp.raise_for_status()

        # Fetch updated issue to confirm
        resp = await client.get(
            f"{YOUTRACK_URL}/api/issues/{issue_id}",
            params={"fields": "idReadable,summary,state(name),assignee(name)"},
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        a = data.get("assignee")
        return (
            f"Updated: **{data.get('idReadable', '?')}** — {data.get('summary', '')}\n"
            f"**State:** {data.get('state', {}).get('name', '?')} | "
            f"**Assignee:** {a.get('name') if a else 'Unassigned'}"
        )


@mcp.tool()
async def get_issue_history(issue_id: str, max_results: int = 20) -> str:
    """Get the change history of a YouTrack issue from the activity log.

    Shows who changed what field, when, and the old/new values.
    Useful for auditing changes or finding values to rollback.

    Args:
        issue_id: Issue ID (e.g., 'DEVOPS-423')
        max_results: Maximum number of activities to return (default: 20)
    """
    from datetime import datetime, timezone

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{YOUTRACK_URL}/api/issues/{issue_id}/activities",
            params={
                "fields": "id,timestamp,author(name),field(name),"
                "added(name,text),removed(name,text)",
                "categories": "CustomFieldCategory,SummaryCategory,DescriptionCategory",
                "$top": str(max_results),
            },
            headers=_headers(),
        )
        resp.raise_for_status()
        activities = resp.json()

        if not activities:
            return f"No change history found for **{issue_id}**."

        def _format_value(val):
            if val is None:
                return "(empty)"
            if isinstance(val, list):
                names = [v.get("name", "") or v.get("text", "") for v in val]
                return ", ".join(names) if names else "(empty)"
            if isinstance(val, str):
                return val[:200] if len(val) > 200 else val
            return str(val)

        lines = [f"## Change history for {issue_id}", ""]
        for a in activities:
            field = a.get("field", {}).get("name", "?")
            added = _format_value(a.get("added"))
            removed = _format_value(a.get("removed"))
            author = a.get("author", {}).get("name", "?")
            ts = datetime.fromtimestamp(
                a["timestamp"] / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")
            activity_id = a.get("id", "?")
            lines.append(
                f"- `{activity_id}` **{field}**: {removed} → {added} "
                f"(by {author}, {ts})"
            )

        return "\n".join(lines)


@mcp.tool()
async def rollback_issue(issue_id: str, activity_id: str) -> str:
    """Rollback a specific change on a YouTrack issue by restoring the previous value.

    Use get_issue_history first to find the activity_id of the change to revert.

    Args:
        issue_id: Issue ID (e.g., 'DEVOPS-423')
        activity_id: Activity ID from get_issue_history (e.g., '0-0.88-598477')
    """
    async with httpx.AsyncClient(timeout=30) as client:
        # Fetch the specific activity to get old value
        resp = await client.get(
            f"{YOUTRACK_URL}/api/issues/{issue_id}/activities",
            params={
                "fields": "id,field(name),added(name,text),removed(name,text)",
                "categories": "CustomFieldCategory,SummaryCategory,DescriptionCategory",
                "$top": 100,
            },
            headers=_headers(),
        )
        resp.raise_for_status()
        activities = resp.json()

        target = None
        for a in activities:
            if a.get("id") == activity_id:
                target = a
                break

        if not target:
            return f"Activity `{activity_id}` not found for **{issue_id}**."

        field_name = target.get("field", {}).get("name", "")
        removed = target.get("removed")  # this is the old value to restore

        # Handle summary rollback via API
        if field_name.lower() == "summary":
            if isinstance(removed, str):
                old_summary = removed
            else:
                return "Cannot determine old summary value."
            resp = await client.post(
                f"{YOUTRACK_URL}/api/issues/{issue_id}",
                json={"summary": old_summary},
                headers={**_headers(), "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            return (
                f"Rolled back **{issue_id}** summary:\n"
                f"**Restored:** {old_summary}"
            )

        # Handle description rollback via API
        if field_name.lower() == "description":
            old_desc = removed if isinstance(removed, str) else ""
            resp = await client.post(
                f"{YOUTRACK_URL}/api/issues/{issue_id}",
                json={"description": old_desc},
                headers={**_headers(), "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            return (
                f"Rolled back **{issue_id}** description to previous version."
            )

        # Handle custom fields (State, Assignee, Priority, etc.) via command
        if isinstance(removed, list) and removed:
            old_value = removed[0].get("name", "")
        elif isinstance(removed, list) and not removed:
            # Field was empty before — need to unset
            old_value = ""
        else:
            old_value = str(removed) if removed else ""

        if not old_value:
            return (
                f"Cannot rollback **{field_name}** — previous value was empty. "
                f"Use `update_issue` to manually set the desired value."
            )

        command = f"{field_name} {old_value}"
        resp = await client.post(
            f"{YOUTRACK_URL}/api/issues/{issue_id}/execute",
            json={"query": command},
            headers={**_headers(), "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return (
            f"Rolled back **{issue_id}**:\n"
            f"**{field_name}:** restored to **{old_value}**"
        )


@mcp.tool()
async def delete_issue(issue_id: str, permanent: bool = False) -> str:
    """Delete a YouTrack issue. By default performs a soft delete (sets state to Obsolete).
    Use permanent=True only when you need to remove the issue entirely — this cannot be undone.

    Args:
        issue_id: Issue ID (e.g., 'DEVOPS-423')
        permanent: If True, permanently delete the issue. If False (default), set state to Obsolete.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        # Fetch issue details first
        resp = await client.get(
            f"{YOUTRACK_URL}/api/issues/{issue_id}",
            params={"fields": "idReadable,summary,state(name)"},
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        summary = data.get("summary", "")
        old_state = data.get("state", {}).get("name", "?")

        if permanent:
            resp = await client.delete(
                f"{YOUTRACK_URL}/api/issues/{issue_id}",
                headers=_headers(),
            )
            resp.raise_for_status()
            return f"Permanently deleted: **{issue_id}** — {summary}"

        # Soft delete: set state to Obsolete
        resp = await client.post(
            f"{YOUTRACK_URL}/api/issues/{issue_id}/execute",
            json={"query": "State Obsolete"},
            headers={**_headers(), "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return (
            f"Soft-deleted: **{issue_id}** — {summary}\n"
            f"**State:** {old_state} → Obsolete"
        )


@mcp.tool()
async def bulk_update_preview(query: str, command: str, max_results: int = 50) -> str:
    """Preview which issues would be affected by a bulk update (dry run).

    Always call this BEFORE bulk_update_execute to review the affected issues.

    Args:
        query: YouTrack search query to select issues (e.g., 'project: DO state: Open')
        command: YouTrack command to apply (e.g., 'State Done', 'Assignee John', 'tag Important')
        max_results: Maximum number of issues to preview (default: 50)
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{YOUTRACK_URL}/api/issues",
            params={
                "query": query,
                "fields": "idReadable,summary,state(name),assignee(name)",
                "$top": str(max_results),
            },
            headers=_headers(),
        )
        resp.raise_for_status()
        issues = resp.json()

        if not issues:
            return f"No issues match query: `{query}`"

        lines = [
            f"## Bulk update preview",
            f"**Query:** `{query}`",
            f"**Command:** `{command}`",
            f"**Issues affected:** {len(issues)}",
            "",
        ]
        for issue in issues:
            assignee = issue.get("assignee", {})
            assignee_name = assignee.get("name", "Unassigned") if assignee else "Unassigned"
            state = issue.get("state", {})
            state_name = state.get("name", "Unknown") if state else "Unknown"
            lines.append(
                f"- **{issue.get('idReadable', '?')}** [{state_name}] "
                f"{issue.get('summary', 'No summary')} → {assignee_name}"
            )

        lines.append("")
        lines.append("⚠ Call `bulk_update_execute` with the same query and command to apply.")
        return "\n".join(lines)


@mcp.tool()
async def bulk_update_execute(query: str, command: str, max_results: int = 50) -> str:
    """Execute a bulk update on issues matching a query. DESTRUCTIVE — call bulk_update_preview first.

    Each batch is tagged with a unique ID (e.g., 'yt-mcp-1741794000') for rollback.
    Use bulk_rollback with the batch tag to undo all changes from a batch.

    Args:
        query: YouTrack search query to select issues (e.g., 'project: DO state: Open')
        command: YouTrack command to apply (e.g., 'State Done', 'Assignee John', 'tag Important')
        max_results: Maximum number of issues to update (default: 50)
    """
    import time

    batch_tag = f"yt-mcp-{int(time.time())}"
    batch_ts = int(time.time() * 1000)  # ms timestamp for activity lookup

    async with httpx.AsyncClient(timeout=30) as client:
        # Fetch matching issues
        resp = await client.get(
            f"{YOUTRACK_URL}/api/issues",
            params={
                "query": query,
                "fields": "idReadable,summary",
                "$top": str(max_results),
            },
            headers=_headers(),
        )
        resp.raise_for_status()
        issues = resp.json()

        if not issues:
            return f"No issues match query: `{query}`"

        # Tag all issues first, then apply command
        tagged = []
        updated = []
        errors = []

        for issue in issues:
            issue_id = issue.get("idReadable", "?")
            try:
                # Add batch tag
                resp = await client.post(
                    f"{YOUTRACK_URL}/api/issues/{issue_id}/execute",
                    json={"query": f"tag {batch_tag}"},
                    headers={**_headers(), "Content-Type": "application/json"},
                )
                resp.raise_for_status()
                tagged.append(issue_id)
            except httpx.HTTPStatusError as e:
                errors.append(f"{issue_id}: tag failed ({e.response.status_code})")

        for issue_id in tagged:
            try:
                resp = await client.post(
                    f"{YOUTRACK_URL}/api/issues/{issue_id}/execute",
                    json={"query": command},
                    headers={**_headers(), "Content-Type": "application/json"},
                )
                resp.raise_for_status()
                updated.append(issue_id)
            except httpx.HTTPStatusError as e:
                errors.append(f"{issue_id}: command failed ({e.response.status_code})")

        lines = [f"## Bulk update complete"]
        lines.append(f"**Batch tag:** `{batch_tag}`")
        lines.append(f"**Command:** `{command}`")
        lines.append(f"**Updated:** {len(updated)} issues")
        if updated:
            lines.append(f"**IDs:** {', '.join(updated)}")
        if errors:
            lines.append(f"\n**Errors ({len(errors)}):**")
            for err in errors:
                lines.append(f"- {err}")
        lines.append("")
        lines.append(f"To undo: `bulk_rollback(batch_tag=\"{batch_tag}\")`")
        return "\n".join(lines)


@mcp.tool()
async def bulk_rollback(batch_tag: str) -> str:
    """Rollback all changes from a bulk update batch.

    Finds all issues tagged with the batch tag, looks up the changes made
    after the tag was applied, and reverts each change to its previous value.

    Args:
        batch_tag: The batch tag from bulk_update_execute (e.g., 'yt-mcp-1741794000')
    """
    from datetime import datetime, timezone

    async with httpx.AsyncClient(timeout=60) as client:
        # Find all issues with the batch tag
        resp = await client.get(
            f"{YOUTRACK_URL}/api/issues",
            params={
                "query": f"tag: {{{batch_tag}}}",
                "fields": "idReadable,summary",
                "$top": 200,
            },
            headers=_headers(),
        )
        resp.raise_for_status()
        issues = resp.json()

        if not issues:
            return f"No issues found with tag `{batch_tag}`. The batch may have already been rolled back."

        # Extract timestamp from tag name
        try:
            batch_ts = int(batch_tag.split("-")[-1]) * 1000
        except (ValueError, IndexError):
            return f"Invalid batch tag format: `{batch_tag}`"

        rolled_back = []
        errors = []

        for issue in issues:
            issue_id = issue.get("idReadable", "?")
            try:
                # Fetch activities after the batch timestamp
                resp = await client.get(
                    f"{YOUTRACK_URL}/api/issues/{issue_id}/activities",
                    params={
                        "fields": "id,timestamp,field(name),"
                        "added(name,text),removed(name,text)",
                        "categories": "CustomFieldCategory,SummaryCategory,"
                        "DescriptionCategory",
                        "$top": 50,
                    },
                    headers=_headers(),
                )
                resp.raise_for_status()
                activities = resp.json()

                # Find changes made around the batch time (within 60s window)
                batch_changes = [
                    a for a in activities
                    if a.get("timestamp", 0) >= batch_ts
                    and a.get("timestamp", 0) <= batch_ts + 60000
                    and a.get("field", {}).get("name", "").lower() != "tag"
                ]

                for change in batch_changes:
                    field_name = change.get("field", {}).get("name", "")
                    removed = change.get("removed")

                    if field_name.lower() == "summary":
                        if isinstance(removed, str) and removed:
                            resp = await client.post(
                                f"{YOUTRACK_URL}/api/issues/{issue_id}",
                                json={"summary": removed},
                                headers={**_headers(), "Content-Type": "application/json"},
                            )
                            resp.raise_for_status()
                            rolled_back.append(f"{issue_id}: summary restored")
                    elif field_name.lower() == "description":
                        old_desc = removed if isinstance(removed, str) else ""
                        resp = await client.post(
                            f"{YOUTRACK_URL}/api/issues/{issue_id}",
                            json={"description": old_desc},
                            headers={**_headers(), "Content-Type": "application/json"},
                        )
                        resp.raise_for_status()
                        rolled_back.append(f"{issue_id}: description restored")
                    else:
                        # Custom field — use command
                        if isinstance(removed, list) and removed:
                            old_value = removed[0].get("name", "")
                        else:
                            old_value = ""
                        if old_value:
                            resp = await client.post(
                                f"{YOUTRACK_URL}/api/issues/{issue_id}/execute",
                                json={"query": f"{field_name} {old_value}"},
                                headers={**_headers(), "Content-Type": "application/json"},
                            )
                            resp.raise_for_status()
                            rolled_back.append(f"{issue_id}: {field_name} → {old_value}")

                # Remove the batch tag
                resp = await client.post(
                    f"{YOUTRACK_URL}/api/issues/{issue_id}/execute",
                    json={"query": f"untag {batch_tag}"},
                    headers={**_headers(), "Content-Type": "application/json"},
                )
                resp.raise_for_status()

            except httpx.HTTPStatusError as e:
                errors.append(f"{issue_id}: {e.response.status_code}")

        lines = [f"## Bulk rollback complete"]
        lines.append(f"**Batch tag:** `{batch_tag}`")
        lines.append(f"**Issues processed:** {len(issues)}")
        lines.append(f"**Changes reverted:** {len(rolled_back)}")
        if rolled_back:
            lines.append("")
            for r in rolled_back:
                lines.append(f"- {r}")
        if errors:
            lines.append(f"\n**Errors ({len(errors)}):**")
            for err in errors:
                lines.append(f"- {err}")
        return "\n".join(lines)


@mcp.tool()
async def create_agile_board(
    name: str,
    projects: str,
    column_field: str = "State",
) -> str:
    """Create a new agile board in YouTrack.

    Args:
        name: Board name (e.g., 'My Sprint Board')
        projects: Comma-separated project short names (e.g., 'DO,BAC')
        column_field: Field to use for columns (default: 'State')
    """
    project_list = [p.strip() for p in projects.split(",")]

    async with httpx.AsyncClient(timeout=30) as client:
        # Resolve project IDs
        project_ids = []
        for short_name in project_list:
            resp = await client.get(
                f"{YOUTRACK_URL}/api/admin/projects",
                params={"query": f"shortName: {short_name}", "fields": "id,shortName"},
                headers=_headers(),
            )
            resp.raise_for_status()
            found = resp.json()
            if found:
                project_ids.append({"id": found[0]["id"]})
            else:
                return f"Project '{short_name}' not found."

        payload = {
            "name": name,
            "projects": project_ids,
            "columnSettings": {
                "field": {"name": column_field},
                "$type": "ColumnSettings",
            },
        }
        resp = await client.post(
            f"{YOUTRACK_URL}/api/agiles",
            json=payload,
            headers={**_headers(), "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        return (
            f"Created board: **{data.get('name', name)}**\n"
            f"**ID:** {data.get('id', '?')}\n"
            f"**Projects:** {', '.join(project_list)}"
        )


def main():
    import argparse

    parser = argparse.ArgumentParser(description="YouTrack MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port to bind to (default: 8000)"
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
