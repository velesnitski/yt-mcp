"""Structured logging and the CLI entry point (ADR-055).

Two modules that run on every invocation and were among the least
covered. Logging is where a leak would reach disk, so what it writes —
and what it refuses to write — is worth pinning rather than assuming.
"""

import json
import logging as stdlib_logging
import sys
from unittest.mock import MagicMock, patch

import pytest

from yt_mcp import logging as ytlog


def _record(**extra):
    rec = stdlib_logging.LogRecord(
        name="yt", level=stdlib_logging.INFO, pathname=__file__, lineno=1,
        msg="", args=(), exc_info=None,
    )
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


class TestJSONFormatter:
    """The operational log line."""

    def _format(self, **extra):
        return json.loads(ytlog.JSONFormatter().format(_record(**extra)))

    def test_always_carries_level_message_and_instance(self):
        entry = self._format()
        assert entry["level"] == "info"
        assert entry["instance"] == ytlog.INSTANCE_ID
        assert "ts" in entry and "msg" in entry

    def test_optional_keys_are_omitted_when_unset(self):
        entry = self._format()
        for key in ("tool", "project", "duration_ms", "error_type", "status"):
            assert key not in entry

    def test_optional_keys_appear_when_set(self):
        entry = self._format(tool="search_issues", project="PROJ",
                             duration_ms=12, error_type="UserInputError",
                             status="error")
        assert entry["tool"] == "search_issues"
        assert entry["project"] == "PROJ"
        assert entry["duration_ms"] == 12
        assert entry["error_type"] == "UserInputError"
        assert entry["status"] == "error"

    def test_zero_duration_is_kept_not_dropped(self):
        """`is not None` rather than truthiness: 0 ms is a measurement."""
        assert self._format(duration_ms=0)["duration_ms"] == 0

    def test_exception_text_is_included_when_present(self):
        try:
            raise ValueError("the cause")
        except ValueError:
            rec = _record()
            rec.exc_info = sys.exc_info()
            entry = json.loads(ytlog.JSONFormatter().format(rec))
        assert entry["exception"] == "the cause"


class TestAnalyticsFormatter:
    """The per-call analytics line."""

    def _format(self, **extra):
        return json.loads(ytlog.AnalyticsFormatter().format(_record(**extra)))

    def test_defaults_when_attributes_are_absent(self):
        entry = self._format()
        assert entry["tool"] == "?"
        assert entry["duration_ms"] == 0
        assert entry["status"] == "ok"

    def test_optional_fields_omitted_when_absent(self):
        entry = self._format(tool="t")
        assert "params" not in entry and "error" not in entry

    def test_non_ascii_is_preserved_not_escaped(self):
        out = ytlog.AnalyticsFormatter().format(_record(tool="t", params={"q": "Ошибка"}))
        assert "Ошибка" in out, "ensure_ascii=False keeps logs readable"

    def test_every_entry_carries_an_instance_id(self):
        assert self._format(tool="t")["instance"] == ytlog.INSTANCE_ID


class TestInstanceId:
    def test_unreadable_home_degrades_to_unknown(self):
        """An id is a convenience; losing it must not stop the server."""
        fake = MagicMock()
        fake.exists.side_effect = OSError("no home")
        with patch.object(ytlog, "_INSTANCE_ID_FILE", fake):
            assert ytlog._get_instance_id() == "unknown"

    def test_existing_id_is_reused(self):
        fake = MagicMock()
        fake.exists.return_value = True
        fake.read_text.return_value = "abc123\n"
        with patch.object(ytlog, "_INSTANCE_ID_FILE", fake):
            assert ytlog._get_instance_id() == "abc123"

    def test_id_is_created_when_missing(self):
        fake, written = MagicMock(), {}
        fake.exists.return_value = False
        fake.write_text.side_effect = lambda v: written.setdefault("v", v)
        with patch.object(ytlog, "_INSTANCE_ID_FILE", fake), \
             patch.object(ytlog, "_INSTANCE_DIR", MagicMock()):
            new_id = ytlog._get_instance_id()
        assert new_id == written["v"]
        assert len(new_id) == 8, "a short id keeps every log line cheap"


class TestCli:
    def _run(self, argv):
        from yt_mcp import server
        with patch.object(sys, "argv", argv):
            return server.main()

    def test_version_flag_exits_zero_and_prints_version(self, capsys):
        from yt_mcp import __version__
        with pytest.raises(SystemExit) as exc:
            self._run(["yt-mcp", "--version"])
        assert exc.value.code == 0
        assert __version__ in capsys.readouterr().out

    def test_unknown_flag_exits_non_zero(self):
        with pytest.raises(SystemExit) as exc:
            self._run(["yt-mcp", "--nonsense"])
        assert exc.value.code != 0

    def test_missing_configuration_fails_loudly(self, capsys, monkeypatch):
        """Starting without credentials must say so, not hang on stdio."""
        for var in ("YOUTRACK_URL", "YOUTRACK_TOKEN", "YOUTRACK_INSTANCES"):
            monkeypatch.delenv(var, raising=False)
        from yt_mcp import server
        with patch.object(sys, "argv", ["yt-mcp"]), \
             patch.object(server, "FastMCP", MagicMock()):
            try:
                server.main()
            except SystemExit as e:
                assert e.code != 0
                combined = capsys.readouterr()
                assert (combined.err + combined.out).strip(), (
                    "exited without telling the operator why"
                )
                return
            except Exception:
                return  # surfaced as an exception, which is also loud
