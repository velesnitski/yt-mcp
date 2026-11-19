"""Each HTTP transport must start and answer MCP (ADR-074).

stdio is what Claude Code runs, so a transport that could not even start
went unnoticed: the server is started here in a real subprocess, on a free
port, exactly as a container would start it.
"""

import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "test", "version": "0"}},
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(tmp_path, transport, host, extra_env=None):
    port = _free_port()
    env = dict(os.environ)
    env.update({
        "YOUTRACK_URL": "https://test.youtrack.cloud",
        "YOUTRACK_TOKEN": "perm:test",
        "YOUTRACK_LOG_FILE": str(tmp_path / "log"),
        "YOUTRACK_ANALYTICS_FILE": str(tmp_path / "analytics"),
        "SENTRY_DSN": "",
    })
    env.update(extra_env or {})
    proc = subprocess.Popen(
        [sys.executable, "-m", "yt_mcp.server", "--transport", transport,
         "--host", host, "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"{transport} exited: {proc.stderr.read().decode()[-800:]}")
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return proc, port
        except OSError:
            time.sleep(0.1)
    proc.kill()
    pytest.fail(f"{transport} did not listen within 20 s")


def _stop(proc):
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _initialize(port, host_header=None):
    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    if host_header:
        headers["Host"] = host_header
    r = httpx.post(f"http://127.0.0.1:{port}/mcp", headers=headers,
                   content=json.dumps(INIT), timeout=10)
    return r


@pytest.mark.parametrize("host,host_header", [
    ("127.0.0.1", None),
    # A container binds 0.0.0.0 and clients reach it by service name.
    ("0.0.0.0", "yt-mcp:8000"),
])
def test_streamable_http_starts_and_initializes(tmp_path, host, host_header):
    proc, port = _start(tmp_path, "streamable-http", host)
    try:
        r = _initialize(port, host_header)
        assert r.status_code == 200, r.text
        assert "youtrack" in r.text
    finally:
        _stop(proc)


def test_localhost_bind_keeps_dns_rebinding_protection(tmp_path):
    proc, port = _start(tmp_path, "streamable-http", "127.0.0.1")
    try:
        assert _initialize(port, "attacker.example:80").status_code != 200
    finally:
        _stop(proc)


def test_sse_starts_and_announces_endpoint(tmp_path):
    proc, port = _start(tmp_path, "sse", "127.0.0.1")
    try:
        with httpx.stream("GET", f"http://127.0.0.1:{port}/sse", timeout=10) as r:
            assert r.status_code == 200
            for line in r.iter_lines():
                if line.startswith("event:"):
                    assert "endpoint" in line
                    break
    finally:
        _stop(proc)


def test_oauth_branch_streamable_http_runs_session_manager(tmp_path):
    """The OAuth branch mounts the MCP app under /auth/verify's Starlette; the
    mounted app's lifespan must still start the session manager. Without
    OAuth credentials the request is refused (401), never a 500."""
    proc, port = _start(tmp_path, "streamable-http", "127.0.0.1", {
        "YOUTRACK_OAUTH_URL": f"http://127.0.0.1:{_free_port()}",
        "YOUTRACK_ACCESS_CODE": "test-code",
    })
    try:
        r = _initialize(port)
        assert r.status_code == 401, r.text
        v = httpx.get(f"http://127.0.0.1:{port}/auth/verify", timeout=10)
        assert v.status_code < 500
    finally:
        _stop(proc)


def test_oauth_app_carries_the_session_manager_lifespan(monkeypatch):
    """In-process: entering the outer app's lifespan must start the mounted
    MCP app's session manager, or every streamable-HTTP request fails."""
    from starlette.testclient import TestClient
    from yt_mcp.server import build_server, http_app

    monkeypatch.setenv("YOUTRACK_URL", "https://test.youtrack.cloud")
    monkeypatch.setenv("YOUTRACK_TOKEN", "perm:test")
    monkeypatch.setenv("YOUTRACK_OAUTH_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("YOUTRACK_ACCESS_CODE", "test-code")
    bundle = build_server()
    app = http_app(bundle, "streamable-http", "127.0.0.1", 8000)
    with TestClient(app):
        assert bundle.mcp.session_manager._task_group is not None
