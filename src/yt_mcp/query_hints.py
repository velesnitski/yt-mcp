"""Actionable hints for YouTrack's opaque query-parse 400 (ADR-064).

YouTrack answers every unparseable search with the same sentence — "Can't
parse search query" — whether the syntax is wrong, a value is unknown, or
the project simply lives on another configured instance. The last case cost
a dozen blind retries in one session: nothing in the error said "wrong
instance". This wrapper re-raises that error with what can be checked
cheaply: which instance actually has each `project:` named in the query
(from the client's cached project list), plus the common syntax causes.

Best-effort by construction: the lookup is time-boxed and any failure in it
re-raises the ORIGINAL error unchanged, so a hint can never mask or replace
the real message.
"""

import asyncio
import functools
import re

from yt_mcp.errors import UserInputError
from yt_mcp.resolver import InstanceResolver

_PARSE_ERROR = "Can't parse search query"
_LOOKUP_TIMEOUT = 3.0

# `project: A, {Two Words}, C` — the value list runs until the next
# `field:` / `#tag` / end. Braced names may contain spaces and commas.
_PROJECT_CLAUSE = re.compile(
    r"\bproject\s*:\s*((?:\{[^}]*\}|[^\s,{}()]+)(?:\s*,\s*(?:\{[^}]*\}|[^\s,{}()]+))*)",
    re.IGNORECASE,
)
_PROJECT_VALUE = re.compile(r"\{([^}]*)\}|([^\s,{}()]+)")

SYNTAX_TIPS = (
    "Common causes: a multi-word value without braces (`State: {In Progress}`); "
    "a user given by display name instead of login (`Assignee: jdoe` — "
    "search_users finds it); a field or value name this project does not "
    "have (get_project_fields lists them)."
)


def project_names(query: str) -> list[str]:
    """Short names referenced by `project:` clauses, in order, de-duplicated."""
    out: list[str] = []
    for clause in _PROJECT_CLAUSE.finditer(query or ""):
        for braced, bare in _PROJECT_VALUE.findall(clause.group(1)):
            name = (braced or bare).strip()
            if name and name.lower() not in (n.lower() for n in out):
                out.append(name)
    return out


async def _project_hints(resolver: InstanceResolver, instance: str, query: str) -> list[str]:
    names = project_names(query)
    if not names:
        return []
    current = resolver.resolve(instance)
    named = resolver.named_clients()
    current_name = next((n for n, c in named if c is current), "")
    listings = await asyncio.gather(*(c.list_projects_cached() for _, c in named))
    where: dict[str, list[str]] = {}
    for (inst, _), projects in zip(named, listings):
        for p in projects:
            where.setdefault((p.get("shortName") or "").lower(), []).append(inst)

    hints = []
    for name in names:
        found = where.get(name.lower(), [])
        if current_name in found:
            continue
        if found:
            others = ", ".join(f'instance="{i}"' for i in found)
            hints.append(
                f"Project `{name}` is not on this instance but exists on another — "
                f"retry with {others}."
            )
        elif resolver.is_multi:
            hints.append(f"No configured instance has a project `{name}` (list_projects shows short names).")
        else:
            hints.append(f"This instance has no project `{name}` (list_projects shows short names).")
    return hints


def with_query_hints(func, resolver: InstanceResolver):
    """Wrap a tool so a query-parse 400 carries the hints above."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except UserInputError as exc:
            query = kwargs.get("query")
            if not isinstance(query, str) or _PARSE_ERROR not in str(exc):
                raise
            try:
                hints = await asyncio.wait_for(
                    _project_hints(resolver, kwargs.get("instance") or "", query),
                    _LOOKUP_TIMEOUT,
                )
            except Exception:  # best-effort: never replace the real error
                hints = []
            raise UserInputError(" ".join([str(exc), *hints, SYNTAX_TIPS])) from exc

    return wrapper
