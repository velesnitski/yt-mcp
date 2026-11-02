"""Last-resort cap on a tool's text reply (ADR-064).

A reply is read by the model in full, and past a few tens of thousands of
characters that costs more time than the API call that produced it. Clients
also enforce their own output limit and reject a reply over it outright.
Tools with a natural narrowing knob (get_issues `description_chars`,
get_article `offset`) apply it themselves; this cap only catches the rest.

Never applied to:
- JSON replies — a cut document is invalid, worse than a long one;
- EXEMPT tools, whose output feeds a write that needs the full text.

Set YOUTRACK_MAX_RESPONSE_CHARS (0 disables; default 60000).
"""

import functools
import os

DEFAULT_MAX_CHARS = 60_000

# get_issues_for_translation output is translated and written back: a cut
# description would be written back cut.
EXEMPT = frozenset({"get_issues_for_translation"})


def _limit_from_env() -> int:
    raw = os.environ.get("YOUTRACK_MAX_RESPONSE_CHARS", "").strip()
    try:
        return int(raw) if raw else DEFAULT_MAX_CHARS
    except ValueError:
        return DEFAULT_MAX_CHARS


MAX_CHARS = _limit_from_env()


def cap_text(text: str, limit: int) -> str:
    """`text` cut at the last line break within `limit`, with a marker."""
    if limit <= 0 or len(text) <= limit or text.lstrip()[:1] in ("{", "["):
        return text
    cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:
        cut = limit
    return (
        text[:cut].rstrip()
        + f"\n\n_[yt-mcp: reply cut at {cut} of {len(text)} characters. "
        "Narrow the request — fewer issues or a tighter query, "
        "include_comments=false, a lower max_results — or ask for format=\"json\".]_"
    )


def capped(func, limit: int | None = None):
    """Wrap a tool so an over-long text reply is cut with a marker."""
    if func.__name__ in EXEMPT:
        return func
    lim = MAX_CHARS if limit is None else limit

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        result = await func(*args, **kwargs)
        return cap_text(result, lim) if isinstance(result, str) else result

    return wrapper
