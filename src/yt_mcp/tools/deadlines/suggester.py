"""suggest_managers tool — bootstrap manager mapping from activity heuristics."""

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from yt_mcp.resolver import InstanceResolver
from yt_mcp.tools.deadlines import config as cfg
from yt_mcp.tools.deadlines import fetcher
from yt_mcp.tools.deadlines.parser import (
    _DONE_STATES,
    _compile_bot_patterns,
    _is_bot,
    _is_deadline_field,
)
from yt_mcp.tools.deadlines.render import render_suggestion


_PM_FANOUT_PERCENTILE = 0.90
_PM_MIN_FANOUT = 6


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def suggest_managers(
        lookback_days: int = 90,
        projects: str = "",
        write: bool = True,
        instance: str = "",
    ) -> str:
        """Bootstrap a manager mapping from recent YouTrack activity.

        Detects PMs by reporter fan-out (auto-exclude), then scores remaining
        candidates by who edits non-trivial fields on each user's tasks and
        who moves their tasks to terminal states. Writes managers.suggested.json.

        Args:
            lookback_days: Activity window (default: 90)
            projects: Comma-separated project shortnames; empty = all accessible
            write: If False, just print the suggestion without saving (default: True)
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        now = datetime.now(tz=timezone.utc)
        policy = cfg._load_policy()
        bot_patterns = _compile_bot_patterns(policy)
        manual_pms: set[str] = set(policy.get("manual_pms") or [])
        operator = await fetcher.get_operator_login(client)
        proj_clause, proj_list = fetcher.build_project_clause(projects)

        date_clause = f"updated: {{minus {lookback_days}d}} .. Today"
        query = (proj_clause + " " + date_clause).strip()

        issues = await client.get(
            "/api/issues",
            params={"query": query, "fields": fetcher.ISSUE_FIELDS, "$top": "500"},
        )
        if not issues:
            return f"## Manager suggester\nNo issues in lookback. Query: `{query}`"

        ids = [i.get("idReadable", "") for i in issues if i.get("idReadable")]
        results = await fetcher.fetch_activities_only_bounded(client, ids)

        # PM detection: top decile of reporter fanout, with a hard floor.
        reporter_assignees: dict[str, set[str]] = defaultdict(set)
        for issue in issues:
            r = (issue.get("reporter") or {}).get("login")
            a = fetcher.extract_assignee_login(issue)
            if r and a and not _is_bot(r, bot_patterns) and not _is_bot(a, bot_patterns):
                reporter_assignees[r].add(a)
        fanouts = sorted(
            ((r, len(s)) for r, s in reporter_assignees.items()),
            key=lambda x: -x[1],
        )
        pms: set[str] = set(manual_pms)
        if fanouts:
            top_n = max(1, int(len(fanouts) * (1 - _PM_FANOUT_PERCENTILE)))
            for r, fanout in fanouts[:top_n]:
                if fanout >= _PM_MIN_FANOUT:
                    pms.add(r)

        # Per-(target, editor) signals.
        field_edits: dict[tuple[str, str], int] = defaultdict(int)
        resolvers: dict[tuple[str, str], int] = defaultdict(int)
        issue_by_id = {i.get("idReadable", ""): i for i in issues}
        for iid, activities in zip(ids, results):
            issue = issue_by_id.get(iid, {})
            target = fetcher.extract_assignee_login(issue)
            if not target or _is_bot(target, bot_patterns):
                continue
            for a in activities:
                fname = (a.get("field") or {}).get("name", "")
                author = (a.get("author") or {}).get("login") or ""
                if not author or author == target or author in pms:
                    continue
                if _is_bot(author, bot_patterns):
                    continue
                if fname == "State":
                    added = a.get("added") or []
                    if isinstance(added, list) and added:
                        new_state = (added[0].get("name") or "").lower()
                        if new_state in _DONE_STATES:
                            resolvers[(target, author)] += 1
                elif fname in ("Priority", "Assignee") or _is_deadline_field(fname):
                    field_edits[(target, author)] += 1

        all_targets = {t for t, _ in field_edits} | {t for t, _ in resolvers}
        for i in issues:
            t = fetcher.extract_assignee_login(i)
            if t and not _is_bot(t, bot_patterns):
                all_targets.add(t)

        suggestion: dict[str, Any] = {}
        for target in sorted(all_targets):
            if target in pms or _is_bot(target, bot_patterns):
                continue
            candidates: dict[str, dict] = {}
            for (t, editor), n in field_edits.items():
                if t == target:
                    candidates.setdefault(editor, {"edits": 0, "resolves": 0})["edits"] = n
            for (t, closer), n in resolvers.items():
                if t == target:
                    candidates.setdefault(closer, {"edits": 0, "resolves": 0})["resolves"] = n
            if not candidates:
                suggestion[target] = {
                    "primary": None,
                    "also_accept": [],
                    "manual_review": True,
                    "evidence": ["no signal in window"],
                }
                continue
            scored = []
            for cand, c in candidates.items():
                reporter_n = sum(
                    1 for i in issues
                    if (i.get("reporter") or {}).get("login") == cand
                    and fetcher.extract_assignee_login(i) == target
                )
                score = 0.55 * c["edits"] + 0.35 * c["resolves"] + 0.10 * reporter_n
                scored.append((cand, score, c["edits"], c["resolves"], reporter_n))
            scored.sort(key=lambda x: -x[1])
            top = scored[0]
            second = scored[1] if len(scored) > 1 else None
            if second and top[1] > 0 and (top[1] - second[1]) / top[1] < 0.25:
                suggestion[target] = {
                    "primary": None,
                    "also_accept": [c[0] for c in scored[:3]],
                    "manual_review": True,
                    "evidence": [
                        f"{c[0]}: edits={c[2]}, resolves={c[3]}, reports={c[4]}"
                        for c in scored[:3]
                    ],
                }
            else:
                suggestion[target] = {
                    "primary": top[0],
                    "also_accept": [c[0] for c in scored[1:3] if c[1] > 0],
                    "manual_review": False,
                    "evidence": [
                        f"{c[0]}: edits={c[2]}, resolves={c[3]}, reports={c[4]}"
                        for c in scored[:3]
                    ],
                }

        # Metadata is namespaced under _metadata to keep it out of user-entry views.
        suggestion["_metadata"] = {
            "generated": now.isoformat(),
            "pms_excluded": sorted(pms),
            "lookback_days": lookback_days,
        }

        written_path = ""
        if write:
            try:
                cfg._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                with cfg._MANAGERS_SUGGESTED_FILE.open("w") as f:
                    json.dump(suggestion, f, indent=2, ensure_ascii=False, sort_keys=True)
                written_path = str(cfg._MANAGERS_SUGGESTED_FILE)
            except OSError as e:
                written_path = f"FAILED: {e}"

        cfg._audit(
            operator, "suggest_managers",
            {"lookback_days": lookback_days, "projects": proj_list},
            len([k for k in suggestion if k != "_metadata"]),
        )
        return render_suggestion(suggestion, operator, lookback_days, written_path, pms)
