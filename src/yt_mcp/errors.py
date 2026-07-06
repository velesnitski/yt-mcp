"""Exception types shared across the client and tools.

Kept dependency-free so both `client.py` and `tools/*` can import it
without cycles.
"""


class YouTrackPermissionError(ValueError):
    """401/403 from YouTrack, raised at the client layer with clean text.

    Subclasses ValueError deliberately: every existing `except ValueError`
    (and `except (httpx.HTTPStatusError, ValueError)`) catch site across the
    tools handles it unchanged — but unlike a raw httpx.HTTPStatusError its
    str() never embeds the request URL, so no tool can leak the instance
    host in its output. 5xx and transport errors still surface as httpx
    exceptions (they are retryable server trouble, not caller trouble).
    """

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(
            f"YouTrack permission error ({status_code}): "
            "insufficient permissions or unauthorized token"
        )
