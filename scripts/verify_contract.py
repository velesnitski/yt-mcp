#!/usr/bin/env python3
"""Probe the live API for the query shapes this server builds (ADR-052).

Every quirk in `docs/YT_QUIRKS.md` is a claim about what the API accepts.
Claims decay: Cloud upgrades change the parser with no notice, and a rule
recorded once and never re-measured is indistinguishable from a mistake —
Q2 asserted a clause ordering the parser had never required, survived two
months, and shaped code in two repos before a retest caught it.

So this asserts both directions:

  * shapes the code RELIES ON must succeed — a regression here means a
    tool is broken right now, before any user reports it;
  * shapes a registry entry calls REJECTED must still be rejected — a
    negative control that starts passing means the quirk is gone and its
    entry is a candidate for retraction.

Read-only: counts only, no writes, no issue bodies fetched.

    YOUTRACK_URL=... YOUTRACK_TOKEN=... uv run python scripts/verify_contract.py

Exits non-zero if any probe disagrees with its expectation.
"""

import os
import sys

import httpx

OK, REJECT = "ok", "reject"


def _fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(2)


def discover_projects(client: httpx.Client, base: str) -> list[str]:
    """Two project keys from recent issues.

    Read from issues rather than the admin API so a plain reporting token
    works, and so no real key is ever hard-coded into this file.
    """
    r = client.get(
        f"{base}/api/issues",
        params={"fields": "idReadable", "$top": "200", "query": "sort by: updated desc"},
    )
    r.raise_for_status()
    keys: list[str] = []
    for issue in r.json():
        iid = issue.get("idReadable") or ""
        if "-" in iid:
            key = iid.rsplit("-", 1)[0]
            if key not in keys:
                keys.append(key)
        if len(keys) >= 2:
            break
    if len(keys) < 2:
        _fail("need two distinct projects in recent issues to probe multi-project shapes")
    return keys[:2]


def build_probes(a: str, b: str) -> list[tuple[str, str, str, str]]:
    """(quirk id, description, query, expectation)."""
    win = "2026-01-01 .. 2026-12-31"
    return [
        # --- shapes the code relies on -----------------------------------
        ("Q17", "project comma-list (multi)", f"project: {a}, {b}", OK),
        ("Q17", "project single", f"project: {a}", OK),
        ("Q1", "canonical resolved attribute", f"project: {a} resolved date: {win}", OK),
        ("Q2", "range clause last", f"project: {a} resolved date: {win}", OK),
        ("Q2", "range clause first", f"resolved date: {win} project: {a}", OK),
        ("Q12", "batch by id, OR-joined", "#{}-1 or #{}-2".format(a, a), OK),
        ("Q11", "summary keyword", f"project: {a} summary: Release", OK),
        ("-", "state comma-list", f"project: {a} State: {{Open}}, {{Closed}}", OK),
        ("-", "updated range", f"project: {a} updated: {win}", OK),
        ("-", "mentions filter", "mentions: me", OK),
        # --- shapes a registry entry calls rejected ----------------------
        ("Q1", "NEGATIVE: resolved: alias with range", f"project: {a} resolved: {win}", REJECT),
        ("Q17", "NEGATIVE: OR-joined project clauses",
         f"(project: {a} or project: {b})", REJECT),
        ("ADR-050", "NEGATIVE: literal due date attribute",
         f"project: {a} due date: {win}", REJECT),
        ("ADR-050", "NEGATIVE: OR-composed ranges",
         f"project: {a} (updated: {win} or resolved date: {win})", REJECT),
    ]


def probe(client: httpx.Client, base: str, query: str) -> tuple[bool, str]:
    try:
        r = client.get(f"{base}/api/issues/count", params={"query": query, "fields": "count"})
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}"
        return True, str(r.json().get("count", "?"))
    except httpx.HTTPError as e:  # transport trouble is not an answer either way
        return False, f"transport: {type(e).__name__}"


def main() -> int:
    base = (os.environ.get("YOUTRACK_URL") or "").rstrip("/")
    token = os.environ.get("YOUTRACK_TOKEN") or ""
    if not base or not token:
        _fail("set YOUTRACK_URL and YOUTRACK_TOKEN")

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    with httpx.Client(headers=headers, timeout=30) as client:
        a, b = discover_projects(client, base)
        print(f"probing {len(build_probes(a, b))} query shapes against the live instance\n")

        mismatches: list[str] = []
        for qid, desc, query, expect in build_probes(a, b):
            accepted, detail = probe(client, base, query)
            got = OK if accepted else REJECT
            agree = got == expect
            mark = "✓" if agree else "✗"
            print(f"  {mark} [{qid:8}] {desc:38} expected {expect:6} got {got:6} ({detail})")
            if not agree:
                mismatches.append(
                    f"[{qid}] {desc}: expected {expect}, got {got} — "
                    + (
                        "a shape the code relies on is now rejected; a tool is broken"
                        if expect == OK
                        else "a shape believed rejected now works; the entry may be retractable"
                    )
                )

    print()
    if mismatches:
        print("MISMATCHES — the registry and the API disagree:")
        for m in mismatches:
            print(f"  - {m}")
        print("\nRe-measure before changing code: isolate ONE variable per probe (Q2's lesson).")
        return 1
    print("all probes agree with the registry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
