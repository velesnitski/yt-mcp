from yt_mcp.client import YouTrackClient
from yt_mcp.templates import ISSUE_TEMPLATES, build_description


def register(mcp, client: YouTrackClient):

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
        result = build_description(template, fields)
        if not result:
            available = ", ".join(ISSUE_TEMPLATES.keys())
            return f"Unknown template '{template}'. Available: {available}"

        template_name, description = result

        project_id = await client.resolve_project_id(project)
        if not project_id:
            return f"Project '{project}' not found."

        data = await client.post(
            "/api/issues",
            json={
                "project": {"id": project_id},
                "summary": summary,
                "description": description,
            },
        )
        issue_id = data.get("idReadable", "?")

        product_str = ""
        if product:
            await client.execute_command(issue_id, f"Product {product}")
            product_str = f"\n**Product:** {product}"

        return (
            f"Created from **{template_name}** template: "
            f"**{issue_id}** — {data.get('summary', '')}{product_str}\n\n"
            f"**Description preview:**\n{description}"
        )
