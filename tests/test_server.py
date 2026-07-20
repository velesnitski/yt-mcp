import json
import subprocess
import sys
import os
import queue
import threading
import time


class TestServerStartup:
    def _run_jsonrpc(self, *messages, timeout=15):
        """Send JSON-RPC messages to the server via stdio and return responses.

        Reads responses until every request (a message carrying an ``id``) has
        been answered, then terminates the process — rather than closing stdin
        and trusting the server to flush all buffered replies before its
        EOF-triggered shutdown. That drain ordering races inside the MCP SDK's
        stdio loop (a late ``tools/list`` reply can be cut off before it is
        written), which made this test flaky across SDK bumps. A real client
        keeps stdin open for the session, so we do too and read replies via a
        background thread — the handshake is then deterministic (ADR-031).
        """
        input_data = "".join(json.dumps(m) + "\n" for m in messages)
        expected_ids = {m["id"] for m in messages if "id" in m}

        env = os.environ.copy()
        env["YOUTRACK_URL"] = "https://test.youtrack.cloud"
        env["YOUTRACK_TOKEN"] = "perm:test-token"

        proc = subprocess.Popen(
            [sys.executable, "-m", "yt_mcp.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
        )
        lines = queue.Queue()

        def _pump(pipe):
            for line in pipe:
                lines.put(line)
            lines.put(None)  # EOF sentinel

        threading.Thread(target=_pump, args=(proc.stdout,), daemon=True).start()

        responses = []
        try:
            proc.stdin.write(input_data)
            proc.stdin.flush()
            seen = set()
            deadline = time.monotonic() + timeout
            while not (expected_ids and expected_ids.issubset(seen)):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    line = lines.get(timeout=remaining)
                except queue.Empty:
                    break
                if line is None:  # server closed stdout / exited
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                responses.append(msg)
                if isinstance(msg, dict) and "id" in msg:
                    seen.add(msg["id"])
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        return responses

    def test_initialize(self):
        responses = self._run_jsonrpc({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0.0"},
                "protocolVersion": "2024-11-05",
            },
        })
        assert len(responses) >= 1
        resp = responses[0]
        assert resp.get("id") == 1
        assert "result" in resp
        # Name includes version suffix so Claude Code's /mcp shows it.
        from yt_mcp import __version__
        assert resp["result"]["serverInfo"]["name"] == f"youtrack v{__version__}"
        # serverInfo.version must be OUR version, not the mcp SDK's (which is
        # what FastMCP reports when the low-level Server.version isn't set).
        assert resp["result"]["serverInfo"]["version"] == __version__

    def test_tools_list(self):
        responses = self._run_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0.0"},
                    "protocolVersion": "2024-11-05",
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
            },
        )
        # Find the tools/list response
        tools_resp = None
        for r in responses:
            if r.get("id") == 2:
                tools_resp = r
                break
        assert tools_resp is not None, f"No tools/list response found in: {responses}"
        tools = tools_resp["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        assert len(tool_names) == 81, f"Expected 81 tools, got {len(tool_names)}"
        # Spot check a few
        assert "search_issues" in tool_names
        assert "get_article" in tool_names
        assert "get_current_user" in tool_names

    def test_tool_has_description(self):
        responses = self._run_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0.0"},
                    "protocolVersion": "2024-11-05",
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
            },
        )
        tools_resp = None
        for r in responses:
            if r.get("id") == 2:
                tools_resp = r
                break
        assert tools_resp is not None
        for tool in tools_resp["result"]["tools"]:
            assert tool.get("description"), f"Tool '{tool['name']}' has no description"

    def test_version_is_silent_and_instant(self):
        """--version must not construct the server: no log lines on stderr,
        nothing but the version on stdout (ADR-024 lazy-startup contract)."""
        result = subprocess.run(
            [sys.executable, "-m", "yt_mcp.server", "--version"],
            capture_output=True, text=True, timeout=30,
        )
        from yt_mcp import __version__
        assert result.stdout.strip() == __version__
        assert "Starting yt-mcp" not in result.stderr

    def test_import_has_no_side_effects(self):
        """Importing yt_mcp.server must not read env config, build clients,
        or register tools — construction lives in build_server()."""
        code = (
            "import logging, yt_mcp.server;"
            "assert not hasattr(yt_mcp.server, 'mcp');"
            "assert not logging.getLogger('yt_mcp').handlers"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr

    def test_build_server_wires_everything(self, monkeypatch):
        monkeypatch.setenv("YOUTRACK_URL", "https://test.youtrack.cloud")
        monkeypatch.setenv("YOUTRACK_TOKEN", "perm:test")
        monkeypatch.delenv("YOUTRACK_INSTANCES", raising=False)
        monkeypatch.delenv("YOUTRACK_OAUTH_URL", raising=False)
        from yt_mcp.server import build_server
        from yt_mcp.tools import _registered_tools
        bundle = build_server()
        assert bundle.oauth_provider is None
        assert len(_registered_tools(bundle.mcp)) == 81
