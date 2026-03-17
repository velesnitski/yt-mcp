import asyncio

from yt_mcp.client import YouTrackClient
from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import _resolve_state, get_product, parse_issue_id


def register(mcp, resolver: InstanceResolver):

    async def _build_impact_graph(
        client: YouTrackClient, root_id: str, depth: int
    ) -> dict:
        """Build a dependency graph starting from root_id.

        Returns {issue_id: {data, relation, source}} for all impacted issues.
        """
        visited: dict[str, dict] = {}
        queue: list[tuple[str, int, str, str]] = [(root_id, 0, "root", "")]

        while queue:
            issue_id, current_depth, relation, source = queue.pop(0)

            if issue_id in visited:
                continue

            # Fetch issue with links and product
            data = await client.get(
                f"/api/issues/{issue_id}",
                params={
                    "fields": "idReadable,summary,state(name),"
                    "customFields(name,value(name)),"
                    "links(direction,linkType(name),"
                    "issues(idReadable,summary,state(name),"
                    "customFields(name,value(name))))",
                },
            )

            visited[issue_id] = {
                "data": data,
                "relation": relation,
                "source": source,
                "depth": current_depth,
            }

            if current_depth >= depth:
                continue

            # Follow links
            for link in data.get("links", []):
                link_type = link.get("linkType", {}).get("name", "?")
                direction = link.get("direction", "?")
                for linked in link.get("issues", []):
                    linked_id = linked.get("idReadable", "")
                    if linked_id and linked_id not in visited:
                        rel = f"{link_type} ({direction})"
                        queue.append((linked_id, current_depth + 1, rel, issue_id))

        # Search for mentions and same-product issues in parallel
        root_product = get_product(visited[root_id]["data"])
        first_product = root_product.split(",")[0].strip() if root_product else ""

        async def _fetch_mentions():
            if root_id in ("", "?"):
                return []
            try:
                return await client.get(
                    "/api/issues",
                    params={
                        "query": root_id,
                        "fields": "idReadable,summary,state(name),"
                        "customFields(name,value(name))",
                        "$top": "20",
                    },
                )
            except (ValueError, Exception):
                return []

        async def _fetch_same_product():
            if not first_product:
                return []
            try:
                return await client.get(
                    "/api/issues",
                    params={
                        "query": f"Product: {{{first_product}}} #Unresolved",
                        "fields": "idReadable,summary,state(name),"
                        "customFields(name,value(name))",
                        "$top": "20",
                    },
                )
            except (ValueError, Exception):
                return []

        mentions, same_product = await asyncio.gather(
            _fetch_mentions(), _fetch_same_product()
        )

        for m in mentions:
            m_id = m.get("idReadable", "")
            if m_id and m_id not in visited and m_id != root_id:
                visited[m_id] = {
                    "data": m,
                    "relation": f"mentions {root_id}",
                    "source": root_id,
                    "depth": 1,
                }

        for sp in same_product:
            sp_id = sp.get("idReadable", "")
            if sp_id and sp_id not in visited and sp_id != root_id:
                visited[sp_id] = {
                    "data": sp,
                    "relation": f"same product ({first_product})",
                    "source": root_id,
                    "depth": 1,
                }

        return visited

    @mcp.tool()
    async def get_impact_map(issue_id: str, depth: int = 2, instance: str = "") -> str:
        """Build a cross-product dependency graph starting from an issue.

        Finds all related issues by following:
        - Issue links (depends on, subtask, relates to) up to N levels deep
        - Product field overlap (unresolved issues sharing the same product)
        - Text mentions (issues referencing this issue ID)

        Args:
            issue_id: Root issue ID (e.g., 'BAC-1828') or YouTrack issue URL
            depth: How many levels of links to follow (default: 2)
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
        graph = await _build_impact_graph(client, issue_id, depth)

        if len(graph) <= 1:
            return f"No related issues found for **{issue_id}**."

        root = graph[issue_id]
        root_data = root["data"]
        root_state = _resolve_state(root_data)
        root_product = get_product(root_data)

        # Group by relation type
        direct_links: list[str] = []
        product_related: list[str] = []
        mentions: list[str] = []

        for iid, info in graph.items():
            if iid == issue_id:
                continue
            data = info["data"]
            state = _resolve_state(data)
            summary = data.get("summary", "?")
            relation = info["relation"]
            project = iid.split("-")[0] if "-" in iid else "?"

            line = f"- **{iid}** [{state}] {summary}"

            if "same product" in relation:
                product_related.append(line)
            elif "mentions" in relation:
                mentions.append(line)
            else:
                line += f" — *{relation}* from {info['source']}"
                direct_links.append(line)

        # Collect unique projects
        all_projects = set()
        for iid in graph:
            if "-" in iid:
                all_projects.add(iid.split("-")[0])

        lines = [
            f"## Impact Map for {issue_id}",
            f"**{root_data.get('summary', '?')}**",
            f"**State:** {root_state}",
        ]
        if root_product:
            lines.append(f"**Product:** {root_product}")
        lines.append("")

        if direct_links:
            lines.append(f"### Direct dependencies ({len(direct_links)})")
            lines.extend(direct_links)
            lines.append("")

        if product_related:
            lines.append(f"### Same product ({len(product_related)})")
            lines.extend(product_related)
            lines.append("")

        if mentions:
            lines.append(f"### Mentions {issue_id} ({len(mentions)})")
            lines.extend(mentions)
            lines.append("")

        total = len(graph) - 1
        lines.append(
            f"**Total impact:** {total} issues across {len(all_projects)} projects"
        )

        return "\n".join(lines)

    @mcp.tool()
    async def get_deadline_impact(issue_id: str, deadline: str = "", instance: str = "") -> str:
        """Analyze what breaks if an issue slips past a deadline.

        Finds all dependent/related issues and categorizes them as:
        - BLOCKED: directly depends on this issue and not yet Done
        - AT RISK: same product or related, not yet Done
        - DONE: already completed, unaffected

        Args:
            issue_id: Root issue ID (e.g., 'BAC-1828') or YouTrack issue URL
            deadline: Optional deadline date (e.g., '2026-03-14'). For context only.
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
        graph = await _build_impact_graph(client, issue_id, depth=2)

        if len(graph) <= 1:
            return f"No related issues found for **{issue_id}**."

        root = graph[issue_id]
        root_data = root["data"]
        root_state = _resolve_state(root_data)

        blocked: list[str] = []
        at_risk: list[str] = []
        done: list[str] = []

        done_states = {"done", "fixed", "verified", "closed", "obsolete", "duplicate"}

        for iid, info in graph.items():
            if iid == issue_id:
                continue

            data = info["data"]
            state = _resolve_state(data)
            summary = data.get("summary", "?")
            relation = info["relation"]
            state_lower = state.lower()

            line = f"- **{iid}** [{state}] {summary}"

            if state_lower in done_states:
                done.append(line)
            elif any(
                kw in relation.lower()
                for kw in ["depends", "is required", "subtask", "parent"]
            ):
                blocked.append(line)
            else:
                at_risk.append(line)

        lines = [f"## Deadline Impact: {issue_id}"]
        if deadline:
            lines.append(f"**Deadline:** {deadline}")
        lines.append(f"**Current state:** {root_state}")
        lines.append(
            f"**{root_data.get('summary', '?')}**"
        )
        lines.append("")

        if deadline:
            lines.append(f"**If not done by {deadline}:**")
        else:
            lines.append("**If this issue slips:**")

        lines.append(
            f"- {len(blocked)} issues **BLOCKED** (directly waiting on this)"
        )
        lines.append(
            f"- {len(at_risk)} issues **AT RISK** (related/same product)"
        )
        lines.append(
            f"- {len(done)} issues **DONE** (unaffected)"
        )
        lines.append("")

        if blocked:
            lines.append(f"### Blocked ({len(blocked)})")
            lines.extend(blocked)
            lines.append("")

        if at_risk:
            lines.append(f"### At Risk ({len(at_risk)})")
            lines.extend(at_risk)
            lines.append("")

        if done:
            lines.append(f"### Done ({len(done)})")
            lines.extend(done)
            lines.append("")

        return "\n".join(lines)
