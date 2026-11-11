"""Suite-wide isolation from the developer's own ~/.yt-mcp.

Several tools read and write files there: the deadline audit log, manager
maps, the deadline policy, at-risk snapshots, and the error and analytics
logs. A test must neither write to them (runs had been appending fake
entries to the real audit log and overwriting the suggested manager map)
nor read them (a real policy or manager map would change test outcomes on
one machine only). Every test gets its own directory instead.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_home_state(tmp_path, monkeypatch):
    from yt_mcp.tools import monitoring
    from yt_mcp.tools.deadlines import config as dcfg

    state = tmp_path / "yt-mcp-home"
    monkeypatch.setattr(dcfg, "_CONFIG_DIR", state)
    monkeypatch.setattr(dcfg, "_MANAGERS_FILE", state / "managers.json")
    monkeypatch.setattr(dcfg, "_MANAGERS_SUGGESTED_FILE", state / "managers.suggested.json")
    monkeypatch.setattr(dcfg, "_POLICY_FILE", state / "policy.json")
    monkeypatch.setattr(dcfg, "_AUDIT_LOG", state / "deadline-audit.log")
    monkeypatch.setattr(monitoring, "_SNAPSHOTS_DIR", state / "snapshots")
    monkeypatch.setenv("YOUTRACK_LOG_FILE", str(state / "yt-mcp.log"))
    monkeypatch.setenv("YOUTRACK_ANALYTICS_FILE", str(state / "analytics.log"))
