from yt_mcp.tools import issues, templates, history, bulk, projects, translate


def register_all(mcp, client):
    issues.register(mcp, client)
    templates.register(mcp, client)
    history.register(mcp, client)
    bulk.register(mcp, client)
    projects.register(mcp, client)
    translate.register(mcp, client)
