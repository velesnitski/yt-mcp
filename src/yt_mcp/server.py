from mcp.server.fastmcp import FastMCP
from yt_mcp.config import load_all_configs
from yt_mcp.client import YouTrackClient
from yt_mcp.resolver import InstanceResolver
from yt_mcp.tools import register_all

mcp = FastMCP("youtrack")

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
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
