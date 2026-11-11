"""Entry point. Deliberately free of import-time side effects (ADR-024):
importing this module must not configure logging, init Sentry, read env
config, build HTTP clients, or register tools. All of that happens in
build_server(), called from main() AFTER argument parsing — so
`yt-mcp --version` answers instantly and silently, and tests can import the
module without environment setup.
"""
import logging
import os
from dataclasses import dataclass, field

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
    clients: dict = field(default_factory=dict)

    async def aclose(self) -> None:
        """Close every client's HTTP pool — for one-shot runs (headless);
        a transport keeps them for the life of the process."""
        for c in self.clients.values():
            await c.aclose()


def build_server(stderr_level: int = logging.INFO) -> ServerBundle:
    """Construct the fully-wired MCP server (logging, Sentry, clients, tools)."""
    logger = setup_logging(stderr_level)
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

    # serverInfo.version otherwise reports the mcp SDK's own version (FastMCP
    # doesn't expose a version kwarg) — misleading for any client that reads
    # it instead of parsing the "youtrack vX.Y.Z" name suffix. Same guarded
    # reach-in pattern as tools._registered_tools: degrade to a no-op, never
    # crash, if the SDK layout shifts.
    low_level = getattr(mcp, "_mcp_server", None)
    if low_level is not None and hasattr(low_level, "version"):
        low_level.version = __version__

    configs = load_all_configs()
    clients = {name: YouTrackClient(cfg) for name, cfg in configs.items()}
    resolver = InstanceResolver(clients)

    # Use the first instance's config for server-level settings
    # (read_only, disabled_tools) — they are replicated across instances.
    server_config = next(iter(configs.values()))
    register_all(mcp, resolver, server_config)
    return ServerBundle(mcp=mcp, oauth_provider=oauth_provider, clients=clients)


def main():
    import argparse
    import sys

    # Headless mode (ADR-065): `yt-mcp run <tool>` / `yt-mcp tools` call
    # the server in-process and exit; dispatch before the transport flags.
    from yt_mcp.headless import is_headless, run_headless
    if is_headless(sys.argv[1:]):
        sys.exit(run_headless(sys.argv[1:]))

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
