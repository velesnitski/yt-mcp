"""Bounded concurrency for per-issue write loops (ADR-064).

Bulk and translation tools touch issues that are independent of each other,
but ran them one after another: 4-6 sequential round trips per issue. This
runs up to `limit` issues at once and returns results in input order, so
reports read exactly as before.

`limit` stays under the client's connection pool (5) and is deliberately
small: these are writes, and the client already retries 429/503.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")

WRITE_CONCURRENCY = 4
# Reads multiplex over one HTTP/2 connection; 16 in flight measured ~10x
# faster than one at a time against YouTrack Cloud (ADR-068). 429/503 are
# retried by the client.
READ_CONCURRENCY = 16


async def bounded_map(
    fn: Callable[[T], Awaitable[R]],
    items: Iterable[T],
    limit: int = WRITE_CONCURRENCY,
) -> list[R]:
    """`[await fn(x) for x in items]`, at most `limit` in flight, input order.

    `fn` is expected to catch its own per-item errors. If one raises anyway,
    every other item still runs to completion before the first exception
    propagates, so no write is left running unobserved after the call.
    """
    sem = asyncio.Semaphore(max(1, limit))

    async def one(x: T) -> R:
        async with sem:
            return await fn(x)

    results = await asyncio.gather(*(one(x) for x in items), return_exceptions=True)
    for r in results:
        if isinstance(r, BaseException):
            raise r
    return results  # type: ignore[return-value]
