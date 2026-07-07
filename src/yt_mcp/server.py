"""Entry point. Deliberately free of import-time side effects (ADR-024):
importing this module must not configure logging, init Sentry, read env
config, build HTTP clients, or register tools. All of that happens in
build_server(), called from main() AFTER argument parsing — so
`yt-mcp --version` answers instantly and silently, and tests can import the
module without environment setup.
"""
import os
from dataclasses import dataclass

from mcp.server.fastmcp import FastMCP
from yt_mcp import __version__
from yt_mcp.config import load_all_configs
from yt_mcp.client import YouTrackClient
from yt_mcp.resolver import InstanceResolver
from yt_mcp.tools import register_all
from yt_mcp.logging import setup_logging, setup_sentry, INSTANCE_ID

# Bake the version into the server name so Claude Code's `/mcp` line shows
# "youtrack v1.12.x ✓ connected" rather than just "youtrack". Same pattern
# as slack-mcp's `server.NewMCPServer("slack v"+version, version, ...)`.
_SERVER_NAME = f"youtrack v{__version__}"


@dataclass
class ServerBundle:
    """Everything main() needs from construction, without module globals."""
    mcp: FastMCP
    oauth_provider: object | None


def build_server() -> ServerBundle:
    """Construct the fully-wired MCP server (logging, Sentry, clients, tools)."""
    logger = setup_logging()
    setup_sentry()
    logger.info("Starting yt-mcp", extra={"instance": INSTANCE_ID})

    oauth_provider = None
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
        oauth_provider = SimpleOAuthProvider(
            access_code=access_code,
            server_url=oauth_url,
        )
        mcp = FastMCP(_SERVER_NAME, auth=auth_settings, auth_server_provider=oauth_provider)
    else:
        mcp = FastMCP(_SERVER_NAME)

    configs = load_all_configs()
    clients = {name: YouTrackClient(cfg) for name, cfg in configs.items()}
    resolver = InstanceResolver(clients)

    # Use the first instance's config for server-level settings
    # (read_only, disabled_tools) — they are replicated across instances.
    server_config = next(iter(configs.values()))
    register_all(mcp, resolver, server_config)
    return ServerBundle(mcp=mcp, oauth_provider=oauth_provider)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="YouTrack MCP server")
    parser.add_argument(
        "--version",
        action="version",
        version=__version__,
        help="Print the version and exit",
    )
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
    args = parser.parse_args()  # --version exits here, before any construction

    bundle = build_server()
    mcp = bundle.mcp

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    # For HTTP transports: add verify route if access code is configured
    if bundle.oauth_provider and os.environ.get("YOUTRACK_ACCESS_CODE"):
        import uvicorn
        from starlette.applications import Starlette
        from starlette.routing import Route, Mount
        from yt_mcp.auth import create_verify_handler

        if args.transport == "sse":
            mcp_app = mcp.sse_app()
        else:
            mcp_app = mcp.streamable_http_app()

        app = Starlette(routes=[
            Route("/auth/verify", create_verify_handler(bundle.oauth_provider), methods=["GET", "POST"]),
            Mount("/", mcp_app),
        ])
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
