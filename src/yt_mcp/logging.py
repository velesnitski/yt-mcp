"""Structured logging with optional Sentry error tracking.

Always logs to stderr in JSON format.
Optional: set SENTRY_DSN to send errors to Sentry (pip install sentry-sdk).
Optional: set YOUTRACK_LOG_FILE to write logs to a file.

Each installation gets a persistent instance_id (UUID) for distinguishing
errors from different machines in Sentry.
"""

import json
import logging
import os
import sys
import uuid
from pathlib import Path

from yt_mcp import __version__

_INSTANCE_DIR = Path.home() / ".yt-mcp"
_INSTANCE_ID_FILE = _INSTANCE_DIR / "instance_id"


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
        # Fallback for read-only filesystems (Docker without volume)
        return "unknown"


INSTANCE_ID = _get_instance_id()


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
            "instance": INSTANCE_ID,
        }
        # Add extra fields (tool, project, etc.)
        for key in ("tool", "project", "duration_ms", "error_type"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = str(record.exc_info[1])
        return json.dumps(entry, ensure_ascii=False)


def setup_logging() -> logging.Logger:
    """Configure logging. Call once at startup."""
    logger = logging.getLogger("yt_mcp")
    logger.setLevel(logging.INFO)

    # Stderr handler (always on)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(JSONFormatter())
    logger.addHandler(stderr_handler)

    # File handler (optional)
    log_file = os.environ.get("YOUTRACK_LOG_FILE")
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(JSONFormatter())
            logger.addHandler(file_handler)
        except OSError as e:
            logger.warning(f"Cannot open log file {log_file}: {e}")

    return logger


def setup_sentry() -> None:
    """Initialize Sentry if SENTRY_DSN is set and sentry-sdk is installed."""
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return

    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=dsn,
            release=f"yt-mcp@{__version__}",
            environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
            traces_sample_rate=0,  # No performance tracing, just errors
            send_default_pii=False,
            before_send=_scrub_event,
        )
        sentry_sdk.set_tag("instance_id", INSTANCE_ID)
        sentry_sdk.set_tag("transport", os.environ.get("YT_MCP_TRANSPORT", "stdio"))

    except ImportError:
        logger = logging.getLogger("yt_mcp")
        logger.info("SENTRY_DSN set but sentry-sdk not installed. Run: pip install sentry-sdk")


def _scrub_event(event: dict, hint: dict) -> dict:
    """Remove sensitive data before sending to Sentry."""
    # Strip any YouTrack tokens or URLs from breadcrumbs/extra
    if "extra" in event:
        for key in list(event["extra"].keys()):
            key_lower = key.lower()
            if any(s in key_lower for s in ("token", "secret", "password", "dsn", "url")):
                event["extra"][key] = "[REDACTED]"
    return event
