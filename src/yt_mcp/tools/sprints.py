import asyncio
from datetime import datetime, timezone
from typing import Any

from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import compact_lines, _resolve_state


async def _resolve_board(client: Any, board_name: str) -> tuple[dict | None, str]:
    """Find an agile board by name (partial match). Returns (board, error_msg)."""
    boards = await client.get(
        "/api/agiles",
        params={"fields": "id,name,sprints(id,name,start,finish,archived)"},
    )
    query_lower = board_name.lower()
    matches = [b for b in boards if query_lower in b.get("name", "").lower()]
    if not matches:
        return None, f"No agile board found matching '{board_name}'."
    if len(matches) > 1:
        names = ", ".join(f"'{b.get('name', '?')}'" for b in matches)
        return None, f"Multiple boards match '{board_name}': {names}. Be more specific."
    return matches[0], ""


def _find_sprint(board: dict, sprint_name: str) -> tuple[dict | None, str]:
    """Find a sprint by name in a board. Returns (sprint, error_msg)."""
    sprint_lower = sprint_name.lower()
    for s in board.get("sprints", []):
        if sprint_lower in s.get("name", "").lower():
            return s, ""
    return None, f"Sprint '{sprint_name}' not found on board '{board.get('name', '?')}'."


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def create_sprint(
        board_name: str,
        sprint_name: str,
        start: str = "",
        finish: str = "",
        instance: str = "",
    ) -> str:
        """Create a new sprint on an agile board.

        Args:
            board_name: Board name (partial match)
            sprint_name: Name for the new sprint
            start: Start date ISO 8601 (optional, e.g. '2025-01-01')
            finish: End date ISO 8601 (optional, e.g. '2025-01-14')
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        board, err = await _resolve_board(client, board_name)
        if not board:
            return err

        body: dict = {"name": sprint_name}
        if start:
            body["start"] = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() * 1000)
        if finish:
            body["finish"] = int(datetime.fromisoformat(finish).replace(tzinfo=timezone.utc).timestamp() * 1000)

        data = await client.post(f"/api/agiles/{board['id']}/sprints", json=body)
        return (
            f"Created sprint: **{data.get('name', sprint_name)}**\n"
            f"**Board:** {board.get('name', '?')}\n"
            f"**ID:** {data.get('id', '?')}"
        )

    @mcp.tool()
    async def update_sprint(
        board_name: str,
        sprint_name: str,
        new_name: str = "",
        start: str = "",
        finish: str = "",
        archived: bool | None = None,
        instance: str = "",
    ) -> str:
        """Update an existing sprint on an agile board.

        Args:
            board_name: Board name (partial match)
            sprint_name: Current sprint name (partial match)
            new_name: New sprint name (empty = keep)
            start: New start date ISO 8601 (empty = keep)
            finish: New end date ISO 8601 (empty = keep)
            archived: Set archived status (None = keep)
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        board, err = await _resolve_board(client, board_name)
        if not board:
            return err

        sprint, err = _find_sprint(board, sprint_name)
        if not sprint:
            return err

        body: dict = {}
        if new_name:
            body["name"] = new_name
        if start:
            body["start"] = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() * 1000)
        if finish:
            body["finish"] = int(datetime.fromisoformat(finish).replace(tzinfo=timezone.utc).timestamp() * 1000)
        if archived is not None:
            body["archived"] = archived

        if not body:
            return "Nothing to update — provide at least one field."

        await client.post(f"/api/agiles/{board['id']}/sprints/{sprint['id']}", json=body)
        display_name = new_name if new_name else sprint.get("name", sprint_name)
        return (
            f"Updated sprint: **{display_name}**\n"
            f"**Board:** {board.get('name', '?')}"
        )

    @mcp.tool()
    async def add_issues_to_sprint(
        board_name: str,
        sprint_name: str,
        issue_ids: str,
        instance: str = "",
    ) -> str:
        """Add issues to a sprint using YouTrack commands.

        Args:
            board_name: Board name (partial match)
            sprint_name: Sprint name (partial match)
            issue_ids: Comma-separated issue IDs (e.g. 'PROJ-1,PROJ-2')
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        board, err = await _resolve_board(client, board_name)
        if not board:
            return err

        sprint, err = _find_sprint(board, sprint_name)
        if not sprint:
            return err

        ids = [pid.strip() for pid in issue_ids.split(",") if pid.strip()]
        if not ids:
            return "No issue IDs provided."

        board_display = board.get("name", board_name)
        sprint_display = sprint.get("name", sprint_name)
        command = f"Board {board_display} {sprint_display}"

        succeeded = []
        failed = []
        for iid in ids:
            try:
                await client.execute_command(iid, command)
                succeeded.append(iid)
            except ValueError as e:
                failed.append(f"{iid}: {e}")

        parts = [f"**Board:** {board_display} — **Sprint:** {sprint_display}"]
        if succeeded:
            parts.append(f"**Added ({len(succeeded)}):** {', '.join(succeeded)}")
        if failed:
            parts.append(f"**Failed ({len(failed)}):** {'; '.join(failed)}")
        return compact_lines(parts)

    @mcp.tool()
    async def get_active_sprint_issues(
        boards: str = "",
        exclude_states: str = "",
        ids_only: bool = False,
        instance: str = "",
    ) -> str:
        """Collect issue IDs across current sprints of all boards (parallel fetch).

        Use for "what's actually in flight" across the org — sprint-based truth,
        not state guessing. Feeds into translation/audit/digest flows.

        Args:
            boards: Comma-separated board names (partial match). Empty = all boards.
            exclude_states: Comma-separated states to skip (e.g. 'Closed,Done').
            ids_only: If True, return just a comma-separated ID list (for piping
                into id-based queries like translation). If False, return grouped
                markdown.
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)

        all_boards = await client.get(
            "/api/agiles",
            params={
                "fields": "id,name,currentSprint(id,name),sprints(id,name,archived)",
            },
        )
        if not all_boards:
            return "No agile boards found."

        # Filter boards by name if requested
        if boards:
            name_filters = [b.strip().lower() for b in boards.split(",") if b.strip()]
            selected = [
                b for b in all_boards
                if any(f in b.get("name", "").lower() for f in name_filters)
            ]
        else:
            selected = all_boards

        # Pick current sprint per board (fall back to latest non-archived)
        to_fetch: list[tuple[dict, dict]] = []
        boards_no_sprint: list[str] = []
        for b in selected:
            cs = b.get("currentSprint")
            if cs and cs.get("id"):
                to_fetch.append((b, cs))
                continue
            active_sprints = [s for s in b.get("sprints", []) if not s.get("archived")]
            if active_sprints:
                to_fetch.append((b, active_sprints[-1]))
            else:
                boards_no_sprint.append(b.get("name", "?"))

        if not to_fetch:
            return f"No boards with active sprints. (Searched {len(selected)} boards.)"

        excl = {s.strip().lower() for s in exclude_states.split(",") if s.strip()}

        async def _fetch_sprint_issues(board: dict, sprint: dict) -> tuple[str, str, list[dict]]:
            try:
                data = await client.get(
                    f"/api/agiles/{board['id']}/sprints/{sprint['id']}",
                    params={
                        "fields": "board(columns(issues(idReadable,summary,"
                        "state(name),assignee(name),customFields(name,value(name)))))",
                    },
                )
                issues: list[dict] = []
                for col in (data.get("board") or {}).get("columns", []):
                    for issue in col.get("issues", []):
                        issues.append(issue)
                return board.get("name", "?"), sprint.get("name", "?"), issues
            except (ValueError, KeyError):
                return board.get("name", "?"), sprint.get("name", "?"), []

        results = await asyncio.gather(*(_fetch_sprint_issues(b, s) for b, s in to_fetch))

        # Dedupe (same issue can appear on multiple boards)
        seen_ids: set[str] = set()
        per_board: list[tuple[str, str, list[dict]]] = []
        all_ids: list[str] = []
        for board_name, sprint_name, issues in results:
            unique = []
            for issue in issues:
                iid = issue.get("idReadable", "")
                if not iid or iid in seen_ids:
                    continue
                state = _resolve_state(issue).lower()
                if state in excl:
                    continue
                seen_ids.add(iid)
                unique.append(issue)
                all_ids.append(iid)
            per_board.append((board_name, sprint_name, unique))

        if ids_only:
            return ", ".join(all_ids) if all_ids else "(no issues)"

        lines = [
            f"## Active sprint issues — {len(all_ids)} unique across {len(to_fetch)} boards",
        ]
        if exclude_states:
            lines.append(f"**Excluded states:** {exclude_states}")
        lines.append("")

        for board_name, sprint_name, issues in sorted(per_board, key=lambda x: -len(x[2])):
            if not issues:
                continue
            lines.append(f"### {board_name} — {sprint_name} ({len(issues)})")
            for issue in issues:
                iid = issue.get("idReadable", "?")
                state = _resolve_state(issue)
                assignee = (issue.get("assignee") or {}).get("name") or "Unassigned"
                summary = (issue.get("summary", "") or "?")[:80]
                lines.append(f"- **{iid}** [{state}] → {assignee} | {summary}")
            lines.append("")

        if boards_no_sprint:
            lines.append(f"_Boards without active sprint: {', '.join(boards_no_sprint)}_")

        return compact_lines(lines)
