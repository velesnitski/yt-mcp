from datetime import datetime, timezone

from yt_mcp.client import YouTrackClient


def register(mcp, client: YouTrackClient):

    @mcp.tool()
    async def search_articles(query: str, max_results: int = 20) -> str:
        """Search YouTrack Knowledge Base articles.

        Args:
            query: Search string (matches article titles and content)
            max_results: Maximum results (default: 20)
        """
        articles = await client.get(
            "/api/articles",
            params={
                "query": query,
                "fields": "id,idReadable,summary,project(shortName),"
                "reporter(fullName),updated",
                "$top": str(max_results),
            },
        )

        if not articles:
            return f"No articles found matching '{query}'."

        lines = [f"**Found: {len(articles)} articles**", ""]
        for a in articles:
            project = a.get("project", {})
            proj_name = project.get("shortName", "?") if project else "?"
            updated_ms = a.get("updated")
            updated_str = ""
            if updated_ms:
                updated_str = f" (updated {datetime.fromtimestamp(updated_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d')})"
            lines.append(
                f"- **{a.get('idReadable', '?')}** [{proj_name}] "
                f"{a.get('summary', 'No title')}{updated_str}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def get_article(article_id: str, include_comments: bool = True) -> str:
        """Get a Knowledge Base article by ID with full content.

        Args:
            article_id: Article ID (e.g., 'PROJ-A-1') or database ID
            include_comments: Whether to include comments (default: True)
        """
        comment_fields = (
            ",comments(id,text,author(fullName),created,pinned)"
            if include_comments else ""
        )
        data = await client.get(
            f"/api/articles/{article_id}",
            params={
                "fields": "id,idReadable,summary,content,"
                "project(shortName,name),reporter(fullName),"
                f"created,updated,updatedBy(fullName),tags(name)"
                f"{comment_fields}",
            },
        )

        project = data.get("project", {})
        proj_str = (
            f"{project.get('shortName', '?')} ({project.get('name', '?')})"
            if project else "?"
        )
        reporter = data.get("reporter", {})
        reporter_name = reporter.get("fullName", "?") if reporter else "?"

        parts = [
            f"# {data.get('idReadable', '?')} — {data.get('summary', 'No title')}",
            f"**Project:** {proj_str}",
            f"**Author:** {reporter_name}",
        ]

        created_ms = data.get("created")
        if created_ms:
            parts.append(
                f"**Created:** "
                f"{datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d')}"
            )

        updated_ms = data.get("updated")
        updated_by = data.get("updatedBy", {})
        if updated_ms:
            by_str = f" by {updated_by.get('fullName', '?')}" if updated_by else ""
            parts.append(
                f"**Updated:** "
                f"{datetime.fromtimestamp(updated_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d')}{by_str}"
            )

        tags = data.get("tags", [])
        if tags:
            parts.append(f"**Tags:** {', '.join(t.get('name', '') for t in tags)}")

        content = data.get("content", "")
        parts.append(f"\n---\n{content or '(empty)'}")

        if include_comments:
            comments = data.get("comments", [])
            if comments:
                parts.append(f"\n---\n## Comments ({len(comments)})\n")
                for c in comments:
                    c_author = c.get("author", {})
                    c_author_name = c_author.get("fullName", "?") if c_author else "?"
                    c_created = c.get("created")
                    c_date = ""
                    if c_created:
                        c_date = datetime.fromtimestamp(
                            c_created / 1000, tz=timezone.utc
                        ).strftime("%Y-%m-%d")
                    pinned = " [pinned]" if c.get("pinned") else ""
                    parts.append(
                        f"**{c_author_name}** ({c_date}){pinned} `{c.get('id', '?')}`:"
                    )
                    parts.append(f"{c.get('text', '(empty)')}\n")

        return "\n".join(parts)

    @mcp.tool()
    async def create_article(
        project: str,
        summary: str,
        content: str = "",
        parent_article_id: str = "",
    ) -> str:
        """Create a new Knowledge Base article.

        Args:
            project: Project short name (e.g., 'DO', 'AP')
            summary: Article title
            content: Article body (markdown supported)
            parent_article_id: Optional parent article ID for nesting (e.g., 'PROJ-A-1')
        """
        project_id = await client.resolve_project_id(project)
        if not project_id:
            return f"Project '{project}' not found."

        payload: dict = {
            "project": {"id": project_id},
            "summary": summary,
            "content": content,
        }
        if parent_article_id:
            payload["parentArticle"] = {"id": parent_article_id}

        data = await client.post(
            "/api/articles",
            json=payload,
        )
        article_id = data.get("idReadable", data.get("id", "?"))
        return f"Created article: **{article_id}** — {data.get('summary', summary)}"

    @mcp.tool()
    async def update_article(
        article_id: str,
        summary: str = "",
        content: str = "",
    ) -> str:
        """Update a Knowledge Base article. Returns previous values for rollback.

        Args:
            article_id: Article ID (e.g., 'PROJ-A-1') or database ID
            summary: New title (empty = keep current)
            content: New content (empty = keep current)
        """
        if not summary and not content:
            return "Nothing to update — provide summary or content."

        old = await client.get(
            f"/api/articles/{article_id}",
            params={"fields": "idReadable,summary,content"},
        )

        payload: dict = {}
        if summary:
            payload["summary"] = summary
        if content:
            payload["content"] = content

        await client.post(f"/api/articles/{article_id}", json=payload)

        parts = [f"Updated article **{old.get('idReadable', article_id)}**:"]
        if summary:
            parts.append(f"**Title:** {old.get('summary', '?')} → {summary}")
        if content:
            old_preview = (old.get("content", "") or "")[:200]
            parts.append(f"**Previous content preview:** {old_preview}...")
        parts.append("")
        parts.append("To restore, call `update_article` with the previous values.")
        return "\n".join(parts)

    @mcp.tool()
    async def delete_article(article_id: str) -> str:
        """Delete a Knowledge Base article. Returns article details for restoration.

        Args:
            article_id: Article ID (e.g., 'PROJ-A-1') or database ID
        """
        old = await client.get(
            f"/api/articles/{article_id}",
            params={
                "fields": "idReadable,summary,content,project(shortName)",
            },
        )
        old_summary = old.get("summary", "?")
        old_project = old.get("project", {})
        old_proj_name = old_project.get("shortName", "?") if old_project else "?"
        old_content = (old.get("content", "") or "")[:500]

        await client.delete(f"/api/articles/{article_id}")
        return (
            f"Deleted article **{old.get('idReadable', article_id)}** — {old_summary}\n"
            f"**Project:** {old_proj_name}\n"
            f"**Content preview:** {old_content}\n\n"
            f"To restore, call `create_article` with the details above."
        )

    @mcp.tool()
    async def add_article_comment(article_id: str, text: str) -> str:
        """Add a comment to a Knowledge Base article.

        Args:
            article_id: Article ID (e.g., 'PROJ-A-1') or database ID
            text: Comment text (markdown supported)
        """
        data = await client.post(
            f"/api/articles/{article_id}/comments",
            json={"text": text},
        )
        comment_id = data.get("id", "?") if data else "?"
        return (
            f"Comment added to article **{article_id}**:\n"
            f"**Comment ID:** `{comment_id}`\n"
            f"> {text[:200]}"
        )

    @mcp.tool()
    async def update_article_comment(
        article_id: str, comment_id: str, text: str
    ) -> str:
        """Update a comment on a Knowledge Base article. Returns previous text for rollback.

        Args:
            article_id: Article ID (e.g., 'PROJ-A-1') or database ID
            comment_id: Comment ID (from get_article)
            text: New comment text (markdown supported)
        """
        old = await client.get(
            f"/api/articles/{article_id}/comments/{comment_id}",
            params={"fields": "text"},
        )
        old_text = old.get("text", "") if old else ""

        await client.post(
            f"/api/articles/{article_id}/comments/{comment_id}",
            json={"text": text},
        )
        return (
            f"Comment `{comment_id}` updated on article **{article_id}**:\n"
            f"**Previous text:** {old_text[:300]}\n"
            f"**New text:** {text[:300]}\n\n"
            f"To restore, call `update_article_comment` with the previous text."
        )

    @mcp.tool()
    async def delete_article_comment(article_id: str, comment_id: str) -> str:
        """Delete a comment from a Knowledge Base article. Returns text for restoration.

        Args:
            article_id: Article ID (e.g., 'PROJ-A-1') or database ID
            comment_id: Comment ID (from get_article)
        """
        old = await client.get(
            f"/api/articles/{article_id}/comments/{comment_id}",
            params={"fields": "text,author(fullName)"},
        )
        old_text = old.get("text", "") if old else ""
        old_author = old.get("author", {}).get("fullName", "?") if old else "?"

        await client.delete(f"/api/articles/{article_id}/comments/{comment_id}")
        return (
            f"Comment `{comment_id}` deleted from article **{article_id}**.\n"
            f"**Author:** {old_author}\n"
            f"**Deleted text:** {old_text[:500]}\n\n"
            f"To restore, call `add_article_comment` with the text above."
        )
