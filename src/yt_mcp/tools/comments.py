from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import parse_issue_id


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def add_comment(issue_id: str, text: str, instance: str = "") -> str:
        """Add a comment to a YouTrack issue.

        Args:
            issue_id: Issue ID or URL
            text: Comment text (markdown)
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
        data = await client.post(
            f"/api/issues/{issue_id}/comments",
            json={"text": text},
        )
        author = data.get("author", {}).get("name", "?") if data else "?"
        return f"Comment added to **{issue_id}** by {author}:\n> {text[:200]}"

    @mcp.tool()
    async def update_comment(issue_id: str, comment_id: str, text: str, instance: str = "") -> str:
        """Update an existing comment. Returns previous text for rollback.

        Args:
            issue_id: Issue ID or URL
            comment_id: Comment ID
            text: New comment text (markdown)
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
        old = await client.get(
            f"/api/issues/{issue_id}/comments/{comment_id}",
            params={"fields": "text"},
        )
        old_text = old.get("text", "") if old else ""

        await client.update_comment(issue_id, comment_id, text)
        return (
            f"Comment `{comment_id}` updated on **{issue_id}**:\n"
            f"**Previous text:** {old_text[:300]}\n"
            f"**New text:** {text[:300]}\n\n"
            f"To restore, call `update_comment` with the previous text."
        )

    @mcp.tool()
    async def delete_comment(issue_id: str, comment_id: str, instance: str = "") -> str:
        """Delete a comment from a YouTrack issue. Returns deleted text for restoration.

        Args:
            issue_id: Issue ID or URL
            comment_id: Comment ID
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
        old = await client.get(
            f"/api/issues/{issue_id}/comments/{comment_id}",
            params={"fields": "text,author(name)"},
        )
        old_text = old.get("text", "") if old else ""
        old_author = old.get("author", {}).get("name", "?") if old else "?"

        await client.delete(f"/api/issues/{issue_id}/comments/{comment_id}")
        return (
            f"Comment `{comment_id}` deleted from **{issue_id}**.\n"
            f"**Author:** {old_author}\n"
            f"**Deleted text:** {old_text[:500]}\n\n"
            f"To restore, call `add_comment` with the text above."
        )
