"""From words to tickets: instance discovery, reference resolution, text
matching (ADR-069).

- `list_instances` — every configured instance with its URL and project keys,
  so a caller can route without a config file of its own.
- `resolve_ticket_refs` — "задача 1947 (proxy health check)" → the ticket,
  verified by meaning across instances. A number that exists is not a match:
  the same number lives in many projects, and the first existing one is a
  confident wrong link.
- `find_issues_for_text` — "devs are on the cache warmup" → candidate tickets
  ranked by shared words, or "no confident match" instead of noise.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

from mcp.types import ToolAnnotations

from yt_mcp import textmatch as tm
from yt_mcp.errors import UserInputError
from yt_mcp.fanout import READ_CONCURRENCY, bounded_map
from yt_mcp.formatters import _resolve_assignee, _resolve_state
from yt_mcp.resolver import InstanceResolver

CONFIDENT = 0.5      # share of context words that must show up, summary-weighted
WEAK = 0.2
MARGIN = 0.2         # best must beat the runner-up by this much to be confident
TEXT_THRESHOLD = 0.34
MAX_KEYS_PER_NUMBER = 60
ISSUE_FIELDS = ("idReadable,summary,description,resolved,updated,project(shortName),"
                "assignee(login,name),customFields(name,value(name,login))")
_SINCE = re.compile(r"^(\d+)\s*([dw])$", re.IGNORECASE)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------- shared


def _routes_from_env() -> list[tuple[str, str, str]]:
    """YOUTRACK_REF_ROUTES="ops:main:OPS,web:second:WEB" → [(word, instance, KEY)].

    Local configuration (never committed): which project a bare number most
    likely belongs to when the surrounding words name a team or product.
    """
    out = []
    for part in os.environ.get("YOUTRACK_REF_ROUTES", "").split(","):
        bits = [b.strip() for b in part.split(":")]
        if len(bits) == 3 and all(bits):
            out.append((bits[0].lower(), bits[1], bits[2].upper()))
    return out


def _url(client, iid: str) -> str:
    return f"{client.base_url.rstrip('/')}/issue/{iid}"


def _brief(name: str, client, issue: dict) -> dict:
    iid = issue.get("idReadable") or "?"
    return {
        "id": iid,
        "instance": name,
        "url": _url(client, iid),
        "summary": issue.get("summary") or "",
        "state": _resolve_state(issue) or "",
        "assignee": _resolve_assignee(issue) or "",
        "resolved": bool(issue.get("resolved")),
        "updated": issue.get("updated") or 0,
    }


def _owner_hit(owner: str, issue: dict) -> bool:
    if not owner:
        return False
    a = issue.get("assignee") or {}
    hay = " ".join(str(x) for x in (a.get("login"), a.get("name"), _resolve_assignee(issue)) if x)
    return bool(tm._hits(tm.content_words(owner) or [owner], tm.stems(hay)))


async def _fetch(client, iid: str) -> dict | None:
    """One issue by ID, or None when it does not exist or is not readable."""
    try:
        data = await client.get(f"/api/issues/{iid}", params={"fields": ISSUE_FIELDS})
    except ValueError:  # 404 / 403 / bad id — all mean "not this one"
        return None
    return data if isinstance(data, dict) and data.get("idReadable") else None


async def _projects_by_instance(resolver: InstanceResolver, only: str = "") -> dict[str, list[str]]:
    named = [(n, c) for n, c in resolver.named_clients() if not only or n == only]
    lists = await bounded_map(lambda nc: nc[1].list_projects_cached(), named)
    return {n: [(p.get("shortName") or "") for p in ps if p.get("shortName")]
            for (n, _), ps in zip(named, lists)}


def _rank(cands: list[tuple[str, object, dict]], words: list[str], owner: str) -> list[dict]:
    ranked = []
    for name, client, issue in cands:
        m = tm.score(words, issue.get("summary") or "", (issue.get("description") or "")[:4000])
        s = m.score
        why = m.why()
        if _owner_hit(owner, issue):
            s = min(1.0, s + 0.25)
            why += "; assignee matches owner"
        b = _brief(name, client, issue)
        b.update(score=round(s, 3), why=why)
        ranked.append(b)
    ranked.sort(key=lambda b: (-b["score"], b["resolved"], -b["updated"]))
    return ranked


def _verdict(ranked: list[dict], have_context: bool) -> str:
    if not ranked:
        return "none"
    if not have_context:
        return "candidates"
    best = ranked[0]["score"]
    second = ranked[1]["score"] if len(ranked) > 1 else 0.0
    if best >= CONFIDENT and best - second >= MARGIN:
        return "confident"
    return "weak" if best >= WEAK else "none"


async def resolve_ref(resolver: InstanceResolver, ref: str, context: str = "",
                      instance_hint: str = "", owner: str = "", keys: list[str] | None = None) -> dict:
    """Resolve one reference to verified candidates. See resolve_ticket_refs."""
    words = tm.content_words(f"{ref} {context}")
    have_context = bool(words or owner)
    clients = dict(resolver.named_clients())
    if instance_hint and instance_hint not in clients:
        raise UserInputError(f"Unknown instance {instance_hint!r}; configured: {', '.join(clients)}")

    explicit = tm.ISSUE_KEY.findall(ref)
    if explicit:
        iid = explicit[0].upper()
        project = iid.rsplit("-", 1)[0]
        where = await _projects_by_instance(resolver, instance_hint)
        targets = [n for n, ks in where.items() if project in ks] or list(where)
        found = await bounded_map(lambda n: _fetch(clients[n], iid), targets)
        cands = [(n, clients[n], i) for n, i in zip(targets, found) if i]
        ranked = _rank(cands, words, owner)
        verdict = "none" if not ranked else (
            "exact" if not have_context or ranked[0]["score"] >= WEAK else "weak")
        return {"ref": ref, "confidence": verdict, "candidates": ranked, "checked": [iid]}

    numbers = tm.BARE_REF.findall(ref) or re.findall(r"\b(\d{1,7})\b", ref)
    if not numbers:
        return {"ref": ref, "confidence": "none", "candidates": [], "checked": [],
                "note": "no ticket number or key in the reference"}
    n = numbers[0]

    # Which (instance, KEY) pairs to try, most likely first.
    tries: list[tuple[str, str]] = []
    if keys:
        inst = instance_hint or resolver.default_name
        tries = [(inst, k.upper()) for k in keys]
    else:
        routes = [r for r in _routes_from_env() if not instance_hint or r[1] == instance_hint]
        routes = [r for r in routes if r[1] in clients]
        ctx = tm.stems(f"{ref} {context}")
        hit = [r for r in routes if any(tm._same(tm.stem(r[0]), s) for s in ctx)]
        for _, inst, key in hit + [r for r in routes if r not in hit]:
            tries.append((inst, key))
        if not tries:
            for inst, ks in (await _projects_by_instance(resolver, instance_hint)).items():
                tries += [(inst, k) for k in ks]
    tries = list(dict.fromkeys(tries))[:MAX_KEYS_PER_NUMBER]

    ids = [(inst, f"{key}-{n}") for inst, key in tries]
    found = await bounded_map(lambda t: _fetch(clients[t[0]], t[1]), ids, limit=READ_CONCURRENCY)
    cands = [(inst, clients[inst], i) for (inst, _), i in zip(ids, found) if i]
    ranked = _rank(cands, words, owner)
    if not have_context:
        # No words to verify against: keep the routing order, rank nothing.
        order = {iid: k for k, (_, iid) in enumerate(ids)}
        ranked.sort(key=lambda b: order.get(b["id"], 0))
    out = {"ref": ref, "confidence": _verdict(ranked, have_context),
           "candidates": ranked, "checked": [iid for _, iid in ids]}
    if out["confidence"] in ("confident",):
        out["candidates"] = ranked[:1]
    elif out["confidence"] == "weak":
        out["candidates"] = ranked[:3]
    elif out["confidence"] == "none" and ranked:
        out["note"] = (f"{len(ranked)} ticket(s) with this number exist but none matches "
                       "the context: " + ", ".join(b["id"] for b in ranked[:5]))
        out["candidates"] = []
    return out


def _since_date(since: str) -> str:
    s = (since or "").strip()
    if _DATE.match(s):
        return s
    m = _SINCE.match(s)
    if not m:
        raise UserInputError(f"since={since!r}: use e.g. 30d, 2w or 2026-10-01")
    days = int(m.group(1)) * (7 if m.group(2).lower() == "w" else 1)
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _project_clause(projects: str) -> str:
    keys = [p.strip() for p in (projects or "").split(",") if p.strip()]
    if not keys:
        return ""
    return "project: " + ", ".join(f"{{{k}}}" if " " in k else k for k in keys)


def _bonus(issue: dict, now_ms: int) -> float:
    b = 0.0 if issue.get("resolved") else 0.05
    if now_ms - (issue.get("updated") or 0) <= 7 * 86_400_000:
        b += 0.05
    return b


async def match_text(resolver, client, name: str, text: str, since: str, projects: str,
                     max_per_text: int, instance: str) -> dict:
    """Candidates for one text. See find_issues_for_text."""
    keys = [k.strip().upper() for k in (projects or "").split(",") if k.strip()]
    # Explicit keys and "task 123" references are resolved, not guessed.
    refs = [m.group(0) for m in tm.ISSUE_KEY.finditer(text)]
    if not refs and keys:
        refs = [m.group(0) for m in tm.BARE_REF.finditer(text)]
    if refs:
        resolved = await bounded_map(
            lambda r: resolve_ref(resolver, r, context=text, instance_hint=name,
                                  keys=None if tm.ISSUE_KEY.search(r) else keys), refs)
        cands = [c for r in resolved for c in r["candidates"]]
        return {"text": text, "match": "reference" if cands else "none",
                "candidates": cands[:max_per_text]}

    words = tm.content_words(text)
    if not words:
        return {"text": text, "match": "none", "candidates": [], "note": "no content words"}
    base = " ".join(x for x in (_project_clause(projects), f"updated: {_since_date(since)} .. Today") if x)
    fields = ISSUE_FIELDS
    # One query per word, unioned: YouTrack's handling of a multi-word
    # free-text phrase is not something to rely on, and inflected words may
    # match on their own where the phrase does not. Ranking happens here.
    batches = await bounded_map(
        lambda w: client.get("/api/issues", params={"query": f"{base} {w}", "fields": fields, "$top": "30"}),
        words[:6])
    seen: dict[str, dict] = {}
    for batch in batches:
        for issue in batch or []:
            seen.setdefault(issue.get("idReadable") or "?", issue)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    ranked = _rank([(name, client, i) for i in seen.values()], words, "")
    for b in ranked:
        issue = seen.get(b["id"], {})
        b["score"] = round(min(1.0, b["score"] + _bonus(issue, now_ms)) if b["score"] else 0.0, 3)
    ranked.sort(key=lambda b: -b["score"])
    good = [b for b in ranked if b["score"] >= TEXT_THRESHOLD][:max_per_text]
    out = {"text": text, "match": "words" if good else "none", "candidates": good}
    if not good:
        out["note"] = f"no confident match among {len(ranked)} candidate(s)"
    return out


def _render_candidate(b: dict) -> str:
    who = f" · {b['assignee']}" if b.get("assignee") else ""
    score = f" · score {b['score']}" if "score" in b else ""
    return (f"  - [{b['id']}]({b['url']}) {b['summary']} — {b['state'] or '?'}{who}"
            f" ({b['instance']}){score}\n    why: {b.get('why', '')}")


def _strip_internal(b: dict) -> dict:
    return {k: v for k, v in b.items() if k not in ("resolved", "updated")}


def register(mcp, resolver: InstanceResolver):

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True))
    async def list_instances(format: str = "report") -> str:
        """List every configured YouTrack instance with its base URL and project keys.

        Use it to learn which instance holds which projects before routing a
        ticket number, or to build `<base_url>/issue/<ID>` links.

        Args:
            format: "report" (default) or "json"
                ([{"name", "base_url", "default", "projects": [...]}]).
        """
        named = resolver.named_clients()
        lists = await bounded_map(lambda nc: nc[1].list_projects_cached(), named)
        rows = [{
            "name": n,
            "base_url": c.base_url,
            "default": n == resolver.default_name,
            "projects": sorted((p.get("shortName") or "") for p in ps if p.get("shortName")),
        } for (n, c), ps in zip(named, lists)]
        if format == "json":
            return json.dumps(rows, ensure_ascii=False)
        lines = [f"## {len(rows)} YouTrack instance(s)"]
        for r in rows:
            mark = " (default)" if r["default"] else ""
            lines.append(f"- **{r['name']}**{mark}: {r['base_url']}")
            lines.append(f"  projects: {', '.join(r['projects']) or '(none readable)'}")
        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True))
    async def resolve_ticket_refs(
        refs: list[str], context: str = "", instance_hint: str = "",
        owner: str = "", format: str = "report",
    ) -> str:
        """Turn spoken ticket references into verified tickets, across instances.

        Each ref is a bare number or a key, ideally with the words said
        around it: "1947 (proxy health check)", "task 2317", "WEB-12". A bare
        number is tried in every likely project (YOUTRACK_REF_ROUTES context
        routes first, else every project of every instance) and each hit is
        checked against the words: an existing number is NOT a match by
        itself. Answers per ref: `confident` (one ticket), `weak` (top
        candidates to pick from), `candidates` (no words given — unverified
        list), `exact` (an explicit key that exists), or `none`.

        Args:
            refs: References, one per item, each with its own nearby words.
            context: Words shared by all refs (meeting topic, team name).
            instance_hint: Only this instance (optional).
            owner: Person the work belongs to; an assignee match adds weight.
            format: "report" (default) or "json".
        """
        if not refs:
            raise UserInputError("refs is empty")
        results = await bounded_map(
            lambda r: resolve_ref(resolver, r, context=context,
                                  instance_hint=instance_hint, owner=owner), list(refs), limit=4)
        if format == "json":
            for r in results:
                r["candidates"] = [_strip_internal(b) for b in r["candidates"]]
            return json.dumps(results, ensure_ascii=False)
        lines = [f"## {len(results)} reference(s)"]
        for r in results:
            lines.append(f"- **{r['ref']}** → {r['confidence']}")
            lines += [_render_candidate(b) for b in r["candidates"]]
            if r.get("note"):
                lines.append(f"  _{r['note']}_")
        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True))
    async def find_issues_for_text(
        texts: list[str], since: str = "30d", projects: str = "",
        max_per_text: int = 3, format: str = "report", instance: str = "",
    ) -> str:
        """Find the tickets that phrases from a status check or meeting talk about.

        One call for a whole digest: each text gets candidates ranked by the
        words it shares with the ticket (summary counts most), recency and
        open state; below a threshold the answer is "no confident match",
        never the top of a noisy search. Explicit keys (WEB-12) and "task 123"
        references (tried against `projects` in order) are resolved directly.
        Russian and English, inflections included.

        Args:
            texts: Phrases, one per item.
            since: Only tickets updated since: "30d" (default), "2w", or a date.
            projects: Comma-separated project keys to search (default: all).
            max_per_text: Candidates per text (default 3).
            format: "report" (default) or "json" (id, url, summary, state,
                assignee, score, why per candidate).
            instance: YouTrack instance (optional).
        """
        if not texts:
            raise UserInputError("texts is empty")
        _since_date(since)  # validate once, up front
        client = resolver.resolve(instance)
        name = next((n for n, c in resolver.named_clients() if c is client), instance)
        results = await bounded_map(
            lambda t: match_text(resolver, client, name, t, since, projects,
                                 max(1, max_per_text), instance), list(texts), limit=4)
        if format == "json":
            for r in results:
                r["candidates"] = [_strip_internal(b) for b in r["candidates"]]
            return json.dumps(results, ensure_ascii=False)
        lines = [f"## Tickets for {len(results)} text(s)"]
        for r in results:
            head = r["text"] if len(r["text"]) <= 80 else r["text"][:77] + "…"
            lines.append(f"- “{head}” → {r['match']}")
            lines += [_render_candidate(b) for b in r["candidates"]]
            if r.get("note"):
                lines.append(f"  _{r['note']}_")
        return "\n".join(lines)
