"""Structured logging and analytics.

Logging (errors/warnings):
- Always to stderr in JSON
- Always to ~/.yt-mcp/yt-mcp.log (override with YOUTRACK_LOG_FILE)

Analytics (every tool call):
- Always to ~/.yt-mcp/analytics.log
- Sentry breadcrumbs (if SENTRY_DSN is set)

Each installation gets a persistent instance_id (UUID).
"""

import functools
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

from yt_mcp import __version__

_INSTANCE_DIR = Path.home() / ".yt-mcp"
_INSTANCE_ID_FILE = _INSTANCE_DIR / "instance_id"
_ANALYTICS_FILE = _INSTANCE_DIR / "analytics.log"

# Keys to extract from tool params for analytics (safe, non-sensitive)
_ANALYTICS_KEYS = frozenset({
    "project", "projects", "query", "issue_id", "instance",
    "limit", "since", "since_minutes", "stale_days",
    "keywords", "creator", "exclude_patterns",
})


def _get_instance_id() -> str:
    """Get or create a persistent instance UUID."""
    try:
        if _INSTANCE_ID_FILE.exists():
            return _INSTANCE_ID_FILE.read_text().strip()
        _INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
        instance_id = str(uuid.uuid4())[:8]
        _INSTANCE_ID_FILE.write_text(instance_id)
        return instance_id
    except OSError:
        return "unknown"


INSTANCE_ID = _get_instance_id()

# Analytics logger (separate from error logger)
_analytics_logger: logging.Logger | None = None


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
            "instance": INSTANCE_ID,
        }
        for key in ("tool", "project", "duration_ms", "error_type", "status"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = str(record.exc_info[1])
        return json.dumps(entry, ensure_ascii=False)


class AnalyticsFormatter(logging.Formatter):
    """Compact JSON formatter for analytics events."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "tool": getattr(record, "tool", "?"),
            "duration_ms": getattr(record, "duration_ms", 0),
            "status": getattr(record, "status", "ok"),
            "instance": INSTANCE_ID,
        }
        params = getattr(record, "params", None)
        if params:
            entry["params"] = params
        return json.dumps(entry, ensure_ascii=False)


def setup_logging() -> logging.Logger:
    """Configure error logging. Call once at startup."""
    logger = logging.getLogger("yt_mcp")
    logger.setLevel(logging.INFO)

    # Stderr handler (always on)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(JSONFormatter())
    logger.addHandler(stderr_handler)

    # File handler (default: ~/.yt-mcp/yt-mcp.log)
    log_file = os.environ.get("YOUTRACK_LOG_FILE", str(_INSTANCE_DIR / "yt-mcp.log"))
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(JSONFormatter())
        logger.addHandler(file_handler)
    except OSError:
        pass

    # Analytics logger (separate file, separate logger)
    global _analytics_logger
    _analytics_logger = logging.getLogger("yt_mcp.analytics")
    _analytics_logger.setLevel(logging.INFO)
    _analytics_logger.propagate = False  # Don't send analytics to error log
    try:
        analytics_file = os.environ.get("YOUTRACK_ANALYTICS_FILE", str(_ANALYTICS_FILE))
        Path(analytics_file).parent.mkdir(parents=True, exist_ok=True)
        ah = logging.FileHandler(analytics_file)
        ah.setFormatter(AnalyticsFormatter())
        _analytics_logger.addHandler(ah)
    except OSError:
        pass

    return logger


def setup_sentry() -> None:
    """Initialize Sentry if SENTRY_DSN is set."""
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return

    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        release=f"yt-mcp@{__version__}",
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        traces_sample_rate=0,
        send_default_pii=False,
        before_send=_scrub_event,
    )
    sentry_sdk.set_tag("instance_id", INSTANCE_ID)
    sentry_sdk.set_tag("transport", os.environ.get("YT_MCP_TRANSPORT", "stdio"))


def _scrub_event(event: dict, hint: dict) -> dict:
    """Remove sensitive data before sending to Sentry."""
    if "extra" in event:
        for key in list(event["extra"].keys()):
            key_lower = key.lower()
            if any(s in key_lower for s in ("token", "secret", "password", "dsn")):
                event["extra"][key] = "[REDACTED]"
    return event


def _extract_params(kwargs: dict) -> dict:
    """Extract safe params for analytics logging."""
    return {k: v for k, v in kwargs.items() if k in _ANALYTICS_KEYS and v}


def _add_sentry_breadcrumb(tool: str, params: dict, duration_ms: int, status: str) -> None:
    """Add tool call as Sentry breadcrumb (visible in error context)."""
    if not os.environ.get("SENTRY_DSN"):
        return
    import sentry_sdk
    sentry_sdk.add_breadcrumb(
        category="tool",
        message=tool,
        data={"params": params, "duration_ms": duration_ms, "status": status},
        level="info" if status == "ok" else "error",
    )


def logged(func):
    """Decorator that logs every tool call to analytics + Sentry breadcrumbs."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        tool_name = func.__name__
        params = _extract_params(kwargs)
        start = time.monotonic()
        status = "ok"

        try:
            result = await func(*args, **kwargs)
            return result
        except Exception:
            status = "error"
            raise
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)

            # Analytics log file
            if _analytics_logger:
                _analytics_logger.info(
                    tool_name,
                    extra={
                        "tool": tool_name,
                        "params": params,
                        "duration_ms": duration_ms,
                        "status": status,
                    },
                )

            # Sentry breadcrumb
            _add_sentry_breadcrumb(tool_name, params, duration_ms, status)

    return wrapper
