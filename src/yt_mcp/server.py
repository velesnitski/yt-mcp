import os

from mcp.server.fastmcp import FastMCP
from yt_mcp.config import load_all_configs
from yt_mcp.client import YouTrackClient
from yt_mcp.resolver import InstanceResolver
from yt_mcp.tools import register_all

_oauth_provider = None


def _build_mcp() -> FastMCP:
    """Build the MCP server, optionally with OAuth for claude.ai connectors."""
    global _oauth_provider
    oauth_url = os.environ.get("YOUTRACK_OAUTH_URL", "")

    if oauth_url:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
        from yt_mcp.auth import SimpleOAuthProvider

        access_code = os.environ.get("YOUTRACK_ACCESS_CODE", "")

        auth_settings = AuthSettings(
            issuer_url=oauth_url,
            resource_server_url=oauth_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["youtrack"],
                default_scopes=["youtrack"],
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
        _oauth_provider = SimpleOAuthProvider(
            access_code=access_code,
            server_url=oauth_url,
        )
        return FastMCP("youtrack", auth=auth_settings, auth_server_provider=_oauth_provider)

    return FastMCP("youtrack")


mcp = _build_mcp()

configs = load_all_configs()
clients = {name: YouTrackClient(cfg) for name, cfg in configs.items()}
resolver = InstanceResolver(clients)

# Use the first instance's config for server-level settings (read_only, disabled_tools)
server_config = next(iter(configs.values()))
register_all(mcp, resolver, server_config)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="YouTrack MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port to bind to (default: 8000)"
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    # For HTTP transports: add verify route if access code is configured
    if _oauth_provider and os.environ.get("YOUTRACK_ACCESS_CODE"):
        import uvicorn
        from starlette.applications import Starlette
        from starlette.routing import Route, Mount
        from yt_mcp.auth import create_verify_handler

        if args.transport == "sse":
            mcp_app = mcp.sse_app()
        else:
            mcp_app = mcp.streamable_http_app()

        app = Starlette(routes=[
            Route("/auth/verify", create_verify_handler(_oauth_provider), methods=["GET", "POST"]),
            Mount("/", mcp_app),
        ])
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
