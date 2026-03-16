from yt_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def get_current_user(instance: str = "") -> str:
        """Get the currently authenticated YouTrack user. Useful to verify the token works.

        Args:
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
        data = await client.get(
            "/api/users/me",
            params={"fields": "id,login,fullName,email,online,banned,avatarUrl"},
        )
        status = ""
        if data.get("banned"):
            status = " (BANNED)"
        elif data.get("online"):
            status = " (online)"

        parts = [
            f"## Current user",
            f"**Name:** {data.get('fullName', '?')}",
            f"**Login:** {data.get('login', '?')}",
        ]
        if data.get("email"):
            parts.append(f"**Email:** {data['email']}")
        parts.append(f"**ID:** {data.get('id', '?')}{status}")
        return "\n".join(parts)

    @mcp.tool()
    async def search_users(query: str, max_results: int = 20, instance: str = "") -> str:
        """Search YouTrack users by name or login. Useful for finding assignees.

        Args:
            query: Search string (matches login, full name, email)
            max_results: Maximum results (default: 20)
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
        users = await client.get(
            "/api/users",
            params={
                "query": query,
                "fields": "id,login,fullName,email,online,banned",
                "$top": str(max_results),
            },
        )

        if not users:
            return f"No users found matching '{query}'."

        lines = [f"**Found: {len(users)} users**", ""]
        for u in users:
            status = ""
            if u.get("banned"):
                status = " [BANNED]"
            name = u.get("fullName", "?")
            login = u.get("login", "?")
            email = u.get("email", "")
            email_str = f" ({email})" if email else ""
            lines.append(f"- **{name}** @{login}{email_str}{status}")
        return "\n".join(lines)
