import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from mcp.types import ToolAnnotations
from yt_mcp import contract
from yt_mcp.errors import UserInputError
from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import compact_lines, _resolve_state


async def _resolve_board(client: Any, board_name: str) -> tuple[dict | None, str]:
    """Find an agile board by name (partial match). Returns (board, error_msg)."""
    boards = await client.get(
        "/api/agiles",
        params={"fields": "id,name,sprints(id,name,start,finish,archived)"},
    )
    query_lower = board_name.lower()
    matches = [b for b in boards if query_lower in (b.get("name") or "").lower()]
    if not matches:
        return None, f"No agile board found matching '{board_name}'."
    if len(matches) > 1:
        names = ", ".join(f"'{b.get('name', '?')}'" for b in matches)
        return None, f"Multiple boards match '{board_name}': {names}. Be more specific."
    return matches[0], ""


def _parse_iso_day(value: str, field: str) -> int:
    """ISO day -> epoch ms, rejecting junk as caller input rather than a crash.

    A bare `fromisoformat` raises ValueError, which is not a UserInputError
    and so is NOT filtered out of error reporting — a caller typo would page
    as a production fault (ADR-036).
    """
    try:
        return int(
            datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000
        )
    except ValueError:
        raise UserInputError(
            f"{field} must be an ISO date like 2026-01-31, got {value!r}"
        ) from None


def _find_sprint(board: dict, sprint_name: str) -> tuple[dict | None, str]:
    """Find a sprint by name. Returns (sprint, error_msg).

    Exact name wins, then a unique substring. An ambiguous substring is an
    error, never a silent pick: "Sprint 1" also matches "Sprint 10", and
    the first hit depends on board order — which would aim a write at a
    sprint the caller never named. `_resolve_board` above already refuses
    ambiguity; this did not.
    """
    wanted = sprint_name.strip().lower()
    sprints = board.get("sprints", []) or []
    board_name = board.get("name") or "?"

    exact = [s for s in sprints if (s.get("name") or "").strip().lower() == wanted]
    if exact:
        return exact[0], ""

    matches = [s for s in sprints if wanted in (s.get("name") or "").lower()]
    if not matches:
        return None, f"Sprint '{sprint_name}' not found on board '{board_name}'."
    if len(matches) > 1:
        names = ", ".join(f"'{s.get('name') or '?'}'" for s in matches)
        return None, (
            f"Multiple sprints match '{sprint_name}' on '{board_name}': {names}. "
            "Be more specific, or use the exact name."
        )
    return matches[0], ""


def register(mcp, resolver: InstanceResolver):

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=False, openWorldHint=True))
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
            body["start"] = _parse_iso_day(start, "start")
        if finish:
            body["finish"] = _parse_iso_day(finish, "finish")

        data = await client.post(
            contract.with_fields(f"/api/agiles/{board['id']}/sprints", "id,name"),
            json=body,
        )
        return (
            f"Created sprint: **{data.get('name', sprint_name)}**\n"
            f"**Board:** {board.get('name', '?')}\n"
            f"**ID:** {data.get('id', '?')}"
        )

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=True, openWorldHint=True))
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
            body["start"] = _parse_iso_day(start, "start")
        if finish:
            body["finish"] = _parse_iso_day(finish, "finish")
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

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=True, openWorldHint=True))
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
            except (httpx.HTTPStatusError, ValueError) as e:
                failed.append(f"{iid}: {e}")

        parts = [f"**Board:** {board_display} — **Sprint:** {sprint_display}"]
        if succeeded:
            parts.append(f"**Added ({len(succeeded)}):** {', '.join(succeeded)}")
        if failed:
            parts.append(f"**Failed ({len(failed)}):** {'; '.join(failed)}")
        return compact_lines(parts)

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True))
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
                if any(f in (b.get("name") or "").lower() for f in name_filters)
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
                boards_no_sprint.append((b.get("name") or "?"))

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
                return (board.get("name") or "?"), (sprint.get("name") or "?"), issues
            except (ValueError, KeyError):
                return (board.get("name") or "?"), (sprint.get("name") or "?"), []

        results = await asyncio.gather(*(_fetch_sprint_issues(b, s) for b, s in to_fetch))

        # Dedupe (same issue can appear on multiple boards)
        seen_ids: set[str] = set()
        per_board: list[tuple[str, str, list[dict]]] = []
        all_ids: list[str] = []
        for board_name, sprint_name, issues in results:
            unique = []
            for issue in issues:
                iid = (issue.get("idReadable") or "")
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
                iid = (issue.get("idReadable") or "?")
                state = _resolve_state(issue)
                assignee = (issue.get("assignee") or {}).get("name") or "Unassigned"
                summary = ((issue.get("summary") or "") or "?")[:80]
                lines.append(f"- **{iid}** [{state}] → {assignee} | {summary}")
            lines.append("")

        if boards_no_sprint:
            lines.append(f"_Boards without active sprint: {', '.join(boards_no_sprint)}_")

        return compact_lines(lines)
