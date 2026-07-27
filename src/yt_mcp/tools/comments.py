from datetime import datetime, timezone

from yt_mcp.errors import UserInputError
from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import compact_lines, escape_query_value, parse_issue_id, split_service_comments


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
        author = (data.get("author") or {}).get("name", "?") if data else "?"
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
        old_author = (old.get("author") or {}).get("name", "?") if old else "?"

        await client.delete(f"/api/issues/{issue_id}/comments/{comment_id}")
        return (
            f"Comment `{comment_id}` deleted from **{issue_id}**.\n"
            f"**Author:** {old_author}\n"
            f"**Deleted text:** {old_text[:500]}\n\n"
            f"To restore, call `add_comment` with the text above."
        )

    @mcp.tool()
    async def find_comments(
        text: str,
        author: str = "",
        project: str = "",
        max_results: int = 10,
        instance: str = "",
    ) -> str:
        """Find issues by what their COMMENTS say.

        Answers "the ticket where someone wrote …". Two stages: a YouTrack
        full-text query narrows candidate issues (pass `author` — a login —
        to add the `commenter:` filter), then comments are matched locally:
        the whole phrase case-insensitively, falling back to all-words when
        the phrase doesn't appear verbatim. Workflow-bot nags and service
        stamps are ignored. Newest matches first.

        Args:
            text: Phrase (or words) to find in comment text — required
            author: Only comments by this login; also narrows the search
            project: Limit to one project key (optional)
            max_results: Max matching comments returned (default: 10)
            instance: YouTrack instance (optional)
        """
        phrase = " ".join(text.split()).lower()
        if not phrase:
            raise UserInputError("text is required")
        words = phrase.split()

        clauses = []
        if project:
            clauses.append(f"project: {escape_query_value(project)}")
        if author:
            clauses.append(f"commenter: {escape_query_value(author)}")
        clauses.append(escape_query_value(text))

        client = resolver.resolve(instance)
        issues = await client.get(
            "/api/issues",
            params={
                "query": " ".join(clauses),
                "fields": "idReadable,summary,comments(text,author(login,name),created)",
                "$top": "40",
            },
        )

        matches: list[dict] = []
        for issue in issues or []:
            comments, _ = split_service_comments(issue.get("comments") or [])
            for c in comments:
                login = (c.get("author") or {}).get("login") or ""
                if author and login.lower() != author.lower():
                    continue
                raw = c.get("text") or ""
                norm = " ".join(raw.split())
                low = norm.lower()
                idx = low.find(phrase)
                if idx < 0 and not all(w in low for w in words):
                    continue
                if idx < 0:
                    idx = low.find(words[0])
                start = max(0, idx - 80)
                end = min(len(norm), (idx if idx >= 0 else 0) + len(phrase) + 160)
                snippet = ("…" if start > 0 else "") + norm[start:end] + ("…" if end < len(norm) else "")
                matches.append({
                    "issue": issue.get("idReadable", "?"),
                    "summary": issue.get("summary") or "",
                    "author": (c.get("author") or {}).get("name") or login or "?",
                    "created": c.get("created") or 0,
                    "snippet": snippet,
                })

        if not matches:
            scope = f" by {author}" if author else ""
            return f"No comments{scope} matching '{text}' found."

        matches.sort(key=lambda m: m["created"], reverse=True)
        lines = [f"## Comments matching '{text}' — {len(matches)} found", ""]
        for m in matches[:max_results]:
            when = (
                datetime.fromtimestamp(m["created"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                if m["created"] else "?"
            )
            lines.append(f"- **{m['issue']}** ({m['summary'][:60]}) — {m['author']}, {when}:")
            lines.append(f"  > {m['snippet']}")
        if len(matches) > max_results:
            lines.append(f"…and {len(matches) - max_results} more — raise max_results or narrow the query.")
        return compact_lines(lines)
