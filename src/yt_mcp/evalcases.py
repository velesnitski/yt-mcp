"""`yt-mcp eval <cases.json>`: run ticket-matching cases in-process (ADR-071).

A case file is local: it holds real tickets and never belongs in a repo.
One server start runs every case in parallel, so a check that took a
`uv run` start-up per case takes seconds. Shape (invented example):

    {"cases": [
      {"name": "spoken ref", "tool": "resolve_ticket_refs",
       "args": {"refs": ["4101 queue restart"], "context": "CORE sync"},
       "expect": {"id": "CORE-4101", "verdict": ["confident", "weak"]}},
      {"name": "no ticket exists", "tool": "find_issues_for_text",
       "args": {"texts": ["quarterly budget"]}, "expect": {"verdict": ["none"]}},
      {"name": "trap", "tool": "resolve_ticket_refs",
       "args": {"refs": ["4101 queue"]}, "expect": {"never": "OPS-4101"}},
      {"name": "batch", "tool": "get_issues", "args": {"ids": "CORE-1,OPS-9"},
       "expect": {"contains": ["CORE-1"], "missing": ["OPS-9"]}}
    ]}

`expect` keys: `id` (top candidate), `verdict` (allowed verdicts), `never`
(an id that must not be the top), `contains` / `missing` (get_issues).
Only the first ref/text of a case is judged. Exit 0 when every case
passes, 1 otherwise, 2 on a usage error.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import TextIO

_JSON_TOOLS = {"resolve_ticket_refs", "find_issues_for_text", "get_issues"}


def load_cases(path: str) -> list[dict]:
    try:
        doc = json.loads(Path(path).expanduser().read_text())
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e.strerror}") from None
    except ValueError as e:
        raise ValueError(f"{path} is not JSON: {e}") from None
    cases = doc.get("cases") if isinstance(doc, dict) else doc
    if not isinstance(cases, list) or not all(isinstance(c, dict) for c in cases):
        raise ValueError(f"{path}: expected {{\"cases\": [ {{name, tool, args, expect}}, … ]}}")
    for i, c in enumerate(cases):
        missing = [k for k in ("tool", "args", "expect") if k not in c]
        if missing:
            raise ValueError(f"case {i} ({c.get('name', '?')}): missing {', '.join(missing)}")
    return cases


def judge(case: dict, payload) -> list[str]:
    """Problems with one case's JSON result; [] means pass."""
    e, errs = case["expect"], []
    if case["tool"] == "get_issues":
        items = payload if isinstance(payload, list) else []
        found = {(i.get("idReadable") or i.get("id") or "").upper() for i in items if "error" not in i}
        missed = {(i.get("id") or "").upper() for i in items if "error" in i}
        errs += [f"{x} not fetched" for x in e.get("contains", []) if x.upper() not in found]
        errs += [f"{x} not reported missing" for x in e.get("missing", []) if x.upper() not in missed]
        return errs
    first = payload[0] if isinstance(payload, list) and payload else {}
    verdict, top = first.get("verdict") or "", first.get("top") or ""
    if "verdict" in e and verdict not in e["verdict"]:
        errs.append(f"verdict {verdict or '?'} not in {e['verdict']}")
    if "id" in e and top != e["id"]:
        errs.append(f"top {top or '-'} != {e['id']}")
    if "never" in e and top == e["never"]:
        errs.append(f"linked the trap {e['never']}")
    return errs


async def run_cases(mcp, cases: list[dict], timeout: float, concurrency: int = 6) -> list[tuple]:
    sem = asyncio.Semaphore(concurrency)

    async def one(case):
        args = dict(case["args"])
        if case["tool"] in _JSON_TOOLS:
            args["format"] = "json"
        t = time.monotonic()
        async with sem:
            try:
                res = await asyncio.wait_for(mcp.call_tool(case["tool"], args), timeout)
                content = res[0] if isinstance(res, tuple) else res
                text = "\n".join(getattr(c, "text", "") for c in content or [])
                payload = json.loads(text)
                errs = judge(case, payload)
            except asyncio.TimeoutError:
                errs, payload = [f"timed out after {timeout:g}s"], None
            except Exception as ex:  # tool error or non-JSON reply
                errs, payload = [f"error: {str(ex)[:160]}"], None
        return case, errs, time.monotonic() - t, payload

    return await asyncio.gather(*(one(c) for c in cases))


def report(results, elapsed: float, stdout: TextIO, as_json: bool, verbose: bool) -> int:
    passed = sum(1 for _, errs, _, _ in results if not errs)
    if as_json:
        json.dump({"passed": passed, "total": len(results), "seconds": round(elapsed, 1),
                   "cases": [{"name": c.get("name", ""), "ok": not errs, "problems": errs,
                              "seconds": round(dt, 2)} for c, errs, dt, _ in results]},
                  stdout, ensure_ascii=False)
        stdout.write("\n")
    else:
        for case, errs, dt, payload in results:
            mark = "PASS" if not errs else "FAIL"
            print(f"{mark}  {case.get('name', '?')[:46]:<46} {dt:5.1f}s  {'; '.join(errs)}", file=stdout)
            if errs and verbose and payload is not None:
                print("      " + json.dumps(payload, ensure_ascii=False)[:600], file=stdout)
        print(f"\n{passed}/{len(results)} passed in {elapsed:.0f}s", file=stdout)
    return 0 if passed == len(results) else 1
