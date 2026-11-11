import re
from datetime import datetime, timezone

from mcp.types import ToolAnnotations
from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import _resolve_state, _resolve_assignee, _get_custom_field, compact_lines, escape_query_value

_AGILE_URL_RE = re.compile(r"/agiles/([\d-]+)")
_BOARD_ID_RE = re.compile(r"^\d+-\d+$")


_FIELDS_SELECTOR = "field(name),canBeEmpty,bundle(values(name,archived))"
# Conditional fields (ADR-067): a field shown only when another field has
# one of `values` (or is empty, with showForNullValue).
_CONDITION_SELECTOR = ",condition(field(field(name)),values(name),showForNullValue)"


def _condition(project_field: dict) -> tuple[str, list[str], bool] | None:
    """(controlling field, values that show this field, shown when empty)."""
    cond = project_field.get("condition") or {}
    controller = ((cond.get("field") or {}).get("field") or {}).get("name") or ""
    if not controller:
        return None
    values = [(v.get("name") or "") for v in cond.get("values") or [] if v.get("name")]
    return controller, values, bool(cond.get("showForNullValue"))


def _condition_label(cond: tuple[str, list[str], bool]) -> str:
    controller, values, show_null = cond
    alts = [" or ".join(values)] if values else []
    if show_null:
        alts.append("empty")
    return f"{controller} = {' or '.join(alts) or '?'}"


def _marker(required: bool, cond) -> str:
    if cond:
        kind = "required" if required else "only"
        return f" **({kind} when {_condition_label(cond)})**"
    return " **(required)**" if required else ""


def _parse_when(when: str) -> dict[str, str]:
    """`"Type=Bug; Product=Alpha"` → {"type": "bug", "product": "alpha"}."""
    out: dict[str, str] = {}
    for part in (when or "").split(";"):
        if not part.strip():
            continue
        key, eq, value = part.partition("=")
        if not eq or not key.strip():
            raise ValueError(
                f"`when` takes Field=Value pairs separated by ';' (got {part.strip()!r}).")
        out[key.strip().lower()] = value.strip().lower()
    return out


def _condition_met(cond, given: dict[str, str]) -> bool:
    """True unless `given` names the controlling field with a value that hides
    this one. A controlling field the caller did not mention is unknown, so
    the field is kept."""
    controller, values, show_null = cond
    if controller.lower() not in given:
        return True
    value = given[controller.lower()]
    if not value:
        return show_null
    return value in (v.lower() for v in values)


def register(mcp, resolver: InstanceResolver):

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True))
    async def list_projects(instance: str = "") -> str:
        """List all accessible YouTrack projects.

        Args:
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        try:
            projects = await client.get(
                "/api/admin/projects",
                params={"fields": "shortName,name,archived,leader(name)"},
            )
        except ValueError:
            projects = await client.get(
                "/api/projects",
                params={"fields": "shortName,name,archived,leader(name)"},
            )
        lines = []
        for p in projects:
            status = " (archived)" if p.get("archived") else ""
            leader = p.get("leader", {})
            leader_name = (leader.get("name") or "?") if leader else "?"
            lines.append(
                f"- **{p.get('shortName', '?')}** — {p.get('name', '?')}{status} (lead: {leader_name})"
            )
        return "\n".join(lines) if lines else "No projects found."

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True))
    async def get_agiles(instance: str = "") -> str:
        """List all agile boards.

        Args:
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        boards = await client.get(
            "/api/agiles",
            params={"fields": "id,name,projects(shortName,name),owner(name)"},
        )
        lines = []
        for b in boards:
            proj_names = ", ".join((p.get("shortName") or "?") for p in b.get("projects", []))
            owner = b.get("owner", {})
            owner_name = (owner.get("name") or "?") if owner else "?"
            lines.append(
                f"- **{b.get('name', '?')}** (projects: {proj_names}, owner: {owner_name})"
            )
        return "\n".join(lines) if lines else "No agile boards found."

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True))
    async def get_agile_board(name: str, instance: str = "") -> str:
        """Get agile board details by name, ID, or URL.

        Args:
            name: Board name, ID, or URL
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance, name)

        url_match = _AGILE_URL_RE.search(name)
        board_id = url_match.group(1) if url_match else None

        if not board_id and _BOARD_ID_RE.match(name.strip()):
            board_id = name.strip()

        fields = (
            "id,name,projects(shortName,name),"
            "owner(name),currentSprint(name),"
            "columnSettings(field(name)),"
            "sprints(name,start,finish,archived)"
        )

        if board_id:
            try:
                board = await client.get(
                    f"/api/agiles/{board_id}",
                    params={"fields": fields},
                )
                matches = [board]
            except ValueError:
                return f"No agile board found with ID '{board_id}'."
        else:
            boards = await client.get(
                "/api/agiles",
                params={"fields": fields},
            )
            query_lower = name.lower()
            matches = [b for b in boards if query_lower in (b.get("name") or "").lower()]

        if not matches:
            return f"No agile board found matching '{name}'."

        lines = []
        for b in matches:
            proj_names = ", ".join(
                f"{p.get('shortName', '?')} ({p.get('name', '?')})"
                for p in b.get("projects", [])
            )
            owner = b.get("owner", {})
            owner_name = (owner.get("name") or "?") if owner else "?"
            col_field = ((b.get("columnSettings") or {}).get("field") or {}).get("name", "?")
            current_sprint = b.get("currentSprint", {})
            sprint_name = (current_sprint.get("name") or "None") if current_sprint else "None"

            lines.append(f"# {b.get('name', '?')}")
            lines.append(f"**ID:** {b.get('id', '?')}")
            lines.append(f"**Owner:** {owner_name}")
            lines.append(f"**Projects:** {proj_names}")
            lines.append(f"**Column field:** {col_field}")
            lines.append(f"**Current sprint:** {sprint_name}")

            sprints = [s for s in b.get("sprints", []) if not s.get("archived")]
            if sprints:
                lines.append(f"\n**Active sprints ({len(sprints)}):**")
                for s in sprints:
                    lines.append(f"- {s.get('name', '?')}")

            lines.append("")

        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True))
    async def get_project_fields(project: str, instance: str = "", when: str = "") -> str:
        """List custom fields for a project with required status and available values.

        Use this before create_issue to discover required fields and valid values.
        A conditional field (shown only for some values of another field) is
        labelled with its condition, e.g. `(required when Type = Bug)`: it
        cannot be set on an issue that does not meet it.

        Args:
            project: Project short name
            instance: YouTrack instance (optional)
            when: Values the new issue will have, e.g. "Type=Tech task" or
                "Type=Bug; Product=Alpha". Fields whose condition these values
                do not meet are left out, so the list is exactly what to set.
        """
        client = resolver.resolve(instance)
        project_id = await client.resolve_project_id(project)
        if not project_id:
            return f"Project '{project}' not found."

        fields_data = []
        for endpoint in (
            f"/api/admin/projects/{project_id}/customFields",
            f"/api/projects/{project_id}/customFields",
        ):
            try:
                try:
                    fields_data = await client.get(
                        endpoint, params={"fields": _FIELDS_SELECTOR + _CONDITION_SELECTOR})
                except ValueError:
                    # Servers without conditional fields reject the
                    # selector; the plain list is still worth having.
                    fields_data = await client.get(endpoint, params={"fields": _FIELDS_SELECTOR})
                if fields_data:
                    break
            except (ValueError, KeyError):
                continue

        if not fields_data:
            try:
                issues = await client.get(
                    "/api/issues",
                    params={
                        "query": f"project: {escape_query_value(project)}",
                        "fields": "customFields(name,value(name))",
                        "$top": "50",
                    },
                )
                seen: dict[str, set[str]] = {}
                for issue in issues:
                    for cf in issue.get("customFields", []):
                        n = (cf.get("name") or "")
                        if not n:
                            continue
                        if n not in seen:
                            seen[n] = set()
                        v = cf.get("value")
                        if v is None:
                            continue
                        if isinstance(v, dict):
                            val = (v.get("name") or "")
                            if val:
                                seen[n].add(val)
                        elif isinstance(v, list):
                            for item in v:
                                val = (item.get("name") or "") if isinstance(item, dict) else ""
                                if val:
                                    seen[n].add(val)
                if seen:
                    lines = [f"## Custom fields for {project} (from existing issues)"]
                    for n, vals in sorted(seen.items()):
                        vals_str = ", ".join(sorted(vals)) if vals else "(no values found)"
                        lines.append(f"- **{n}**: {vals_str}")
                    lines.append("\n*Note: required status unavailable, values based on existing issues.*")
                    return "\n".join(lines)
            except (ValueError, KeyError):
                pass
            return f"Cannot fetch custom fields for project '{project}'."

        try:
            given = _parse_when(when)
        except ValueError as e:
            return str(e)

        lines = [f"## Custom fields for {project}"]
        hidden: list[str] = []
        for f in fields_data:
            field_info = f.get("field") or {}
            name = (field_info.get("name") or "?")
            required = not f.get("canBeEmpty", True)
            cond = _condition(f)
            if cond and given and not _condition_met(cond, given):
                hidden.append(name)
                continue
            marker = _marker(required, cond)

            bundle = f.get("bundle")
            if bundle and bundle.get("values"):
                values = [
                    (v.get("name") or "?")
                    for v in bundle["values"]
                    if not v.get("archived")
                ]
                vals_str = ", ".join(values) if values else "(no active values)"
                lines.append(f"- **{name}**{marker}: {vals_str}")
            else:
                lines.append(f"- **{name}**{marker}")

        if hidden:
            lines.append(
                f"\n_Not applicable when {when.strip()} (cannot be set): {', '.join(hidden)}_")
        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=False, openWorldHint=True))
    async def create_agile_board(
        name: str,
        projects: str,
        column_field: str = "State",
        instance: str = "",
    ) -> str:
        """Create a new agile board.

        Args:
            name: Board name
            projects: Comma-separated project short names
            column_field: Column field (default: 'State')
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        project_list = [p.strip() for p in projects.split(",")]

        project_ids = []
        for short_name in project_list:
            pid = await client.resolve_project_id(short_name)
            if not pid:
                return f"Project '{short_name}' not found."
            project_ids.append({"id": pid})

        data = await client.post(
            "/api/agiles",
            json={
                "name": name,
                "projects": project_ids,
                "columnSettings": {
                    "field": {"name": column_field},
                    "$type": "ColumnSettings",
                },
            },
        )
        return (
            f"Created board: **{data.get('name', name)}**\n"
            f"**ID:** {data.get('id', '?')}\n"
            f"**Projects:** {', '.join(project_list)}"
        )

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=True, openWorldHint=True))
    async def delete_agile_board(board_name: str, instance: str = "") -> str:
        """Delete an agile board (issues are not deleted).

        Args:
            board_name: Board name (partial match)
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        boards = await client.get(
            "/api/agiles",
            params={"fields": "id,name,projects(shortName)"},
        )

        query_lower = board_name.lower()
        matches = [b for b in boards if query_lower in (b.get("name") or "").lower()]

        if not matches:
            return f"No agile board found matching '{board_name}'."

        if len(matches) > 1:
            names = ", ".join(f"'{b.get('name', '?')}'" for b in matches)
            return f"Multiple boards match '{board_name}': {names}. Be more specific."

        board = matches[0]
        board_id = board["id"]
        board_display_name = board.get("name", board_name)
        proj_names = ", ".join(
            (p.get("shortName") or "?") for p in board.get("projects", [])
        )

        await client.delete(f"/api/agiles/{board_id}")
        return (
            f"Deleted agile board: **{board_display_name}**\n"
            f"**Projects:** {proj_names}\n\n"
            f"Issues are not affected. To restore, call `create_agile_board`."
        )

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True))
    async def get_sprint_board(board_name: str, sprint: str = "current", instance: str = "") -> str:
        """Get issues on an agile board grouped by column.

        Args:
            board_name: Board name, ID, or URL
            sprint: Sprint name or 'current' (default: 'current')
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance, board_name)

        url_match = _AGILE_URL_RE.search(board_name)
        resolved_id = url_match.group(1) if url_match else None

        if not resolved_id and _BOARD_ID_RE.match(board_name.strip()):
            resolved_id = board_name.strip()

        fields = "id,name,currentSprint(id,name),sprints(id,name,start,finish,archived)"

        if resolved_id:
            try:
                board = await client.get(
                    f"/api/agiles/{resolved_id}",
                    params={"fields": fields},
                )
            except ValueError:
                return f"No agile board found with ID '{resolved_id}'."
        else:
            boards = await client.get(
                "/api/agiles",
                params={"fields": fields},
            )
            query_lower = board_name.lower()
            matches = [b for b in boards if query_lower in (b.get("name") or "").lower()]
            if not matches:
                return f"No agile board found matching '{board_name}'."
            board = matches[0]

        board_id = board["id"]
        board_display_name = board.get("name", board_name)

        sprint_id = None
        sprint_name = None

        if sprint.lower() == "current":
            cs = board.get("currentSprint")
            if cs:
                sprint_id = cs.get("id")
                sprint_name = (cs.get("name") or "Current Sprint")
            else:
                active = [s for s in board.get("sprints", []) if not s.get("archived")]
                if active:
                    sprint_id = active[-1].get("id")
                    sprint_name = active[-1].get("name", "?")
                else:
                    return f"No current sprint found for board '{board_display_name}'."
        else:
            sprint_lower = sprint.lower()
            for s in board.get("sprints", []):
                if sprint_lower in (s.get("name") or "").lower():
                    sprint_id = s.get("id")
                    sprint_name = s.get("name", sprint)
                    break
            if not sprint_id:
                return f"Sprint '{sprint}' not found on board '{board_display_name}'."

        # Issues live on the SPRINT entity, not under board.columns — YT
        # silently ignores an `issues` selector nested in columns (no key
        # comes back at all), which rendered every column as empty while
        # the sprint actually held dozens of issues (ADR-038). Fetch the
        # flat sprint issue list and group into columns client-side.
        sprint_data = await client.get(
            f"/api/agiles/{board_id}/sprints/{sprint_id}",
            params={
                "fields": "name,start,finish,"
                "issues(idReadable,summary,assignee(name),"
                "customFields(name,value(name))),"
                "board(columns(presentation))",
            },
        )

        start = sprint_data.get("start")
        finish = sprint_data.get("finish")
        date_range = ""
        if start and finish:
            start_str = datetime.fromtimestamp(start / 1000, tz=timezone.utc).strftime("%b %d")
            finish_str = datetime.fromtimestamp(finish / 1000, tz=timezone.utc).strftime("%b %d")
            date_range = f" ({start_str} - {finish_str})"

        parts = [f"## {board_display_name} — {sprint_name}{date_range}"]

        all_issues = sprint_data.get("issues") or []
        board_data = sprint_data.get("board") or {}
        columns = board_data.get("columns") or []

        if not all_issues:
            parts.append("\nNo issues in this sprint.")
            return "\n".join(parts)

        # Group by State into the board's column order; states the board
        # doesn't render as a column go to an explicit bucket — never
        # silently dropped.
        col_names = [(c.get("presentation") or "?") for c in columns]
        buckets: dict[str, list] = {name: [] for name in col_names}
        other: list = []
        for issue in all_issues:
            state = _resolve_state(issue)
            target = next(
                (name for name in col_names if name.lower() == (state or "").lower()),
                None,
            )
            (buckets[target] if target else other).append(issue)

        def _render(issues: list) -> None:
            for issue in issues:
                assignee_name = _resolve_assignee(issue)
                parts.append(
                    f"- {issue.get('idReadable', '?')} "
                    f"{issue.get('summary', 'No summary')} "
                    f"→ {assignee_name}"
                )

        for name in col_names:
            parts.append(f"\n### {name} ({len(buckets[name])})")
            _render(buckets[name])
        if other:
            parts.append(f"\n### (states without a column) ({len(other)})")
            _render(other)

        return "\n".join(parts)
