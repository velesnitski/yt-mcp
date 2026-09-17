"""Tests for get_work_items: text truncation (token cost) and date filters."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from yt_mcp.tools import history


def _ms(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def _item(minutes, day, text="", author="Alice"):
    return {
        "id": f"111-{day}",
        "date": _ms(2026, 6, day),
        "duration": {"minutes": minutes},
        "author": {"name": author},
        "text": text,
        "type": {"name": "Development"},
    }


def _setup(response):
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    history.register(mcp, resolver)
    return client, mcp._tool_manager._tools["get_work_items"].fn


class TestTextTruncation:
    async def test_long_text_truncated_by_default(self):
        long_text = "word " * 200  # ~1000 chars of work journal
        client, fn = _setup([_item(60, 17, long_text)])
        out = await fn(issue_id="PROJ-1")
        assert len(out) < 600
        assert "include_text=True for full" in out
        assert f"+{len(long_text) - 200} chars" in out

    async def test_include_text_true_keeps_full_text(self):
        long_text = "word " * 200
        client, fn = _setup([_item(60, 17, long_text)])
        out = await fn(issue_id="PROJ-1", include_text=True)
        assert long_text.rstrip() in out
        assert "include_text=True for full" not in out

    async def test_short_text_never_truncated(self):
        client, fn = _setup([_item(60, 17, "quick fix")])
        out = await fn(issue_id="PROJ-1")
        assert "quick fix" in out
        assert "chars" not in out


class TestDateFilters:
    async def test_since_until_bound_inclusive(self):
        client, fn = _setup([
            _item(60, 10),
            _item(30, 17),
            _item(15, 25),
        ])
        out = await fn(issue_id="PROJ-1", since="2026-06-17", until="2026-06-17")
        assert "2026-06-17" in out
        assert "2026-06-10" not in out and "2026-06-25" not in out
        assert "**Total:** 30m" in out

    async def test_no_filter_keeps_all(self):
        client, fn = _setup([_item(60, 10), _item(30, 17)])
        out = await fn(issue_id="PROJ-1")
        assert "**Total:** 1h 30m" in out

    async def test_empty_after_filter_mentions_period(self):
        client, fn = _setup([_item(60, 10)])
        out = await fn(issue_id="PROJ-1", since="2026-06-20")
        assert "No work items found" in out
        assert "2026-06-20" in out

    async def test_bad_date_rejected(self):
        client, fn = _setup([])
        with pytest.raises(ValueError, match="since must be YYYY-MM-DD"):
            await fn(issue_id="PROJ-1", since="June 17")


# --- work-type resolution (ADR-048) -----------------------------------------

_TYPES = [
    {"id": "251-9", "name": "Development"},
    {"id": "251-10", "name": "Review"},
    {"id": "251-11", "name": "1-on-1"},
]


def _setup_add(types=None, get_raises=None):
    """add_work_item with a mocked type catalogue.

    `types` is what GET on the work-item-types endpoint returns;
    `get_raises` makes that GET fail, standing in for a token without
    admin rights.
    """
    client = MagicMock()
    client.post = AsyncMock(return_value={"id": "111-1"})
    if get_raises is not None:
        client.get = AsyncMock(side_effect=get_raises)
    else:
        client.get = AsyncMock(return_value=types if types is not None else _TYPES)
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    history.register(mcp, resolver)
    return client, mcp._tool_manager._tools["add_work_item"].fn


def _sent_type(client):
    return client.post.call_args.kwargs["json"].get("type")


class TestWorkTypeResolution:
    """A name must reach YouTrack as an id: this endpoint will not look a
    WorkItemType up by name."""

    async def test_name_is_resolved_to_id(self):
        client, fn = _setup_add()
        await fn(issue_id="PROJ-1", duration_minutes=30, work_type="Development")
        assert _sent_type(client) == {"id": "251-9"}

    async def test_name_match_is_case_and_space_insensitive(self):
        client, fn = _setup_add()
        await fn(issue_id="PROJ-1", duration_minutes=30, work_type="  review ")
        assert _sent_type(client) == {"id": "251-10"}

    async def test_entity_id_passes_through_without_a_lookup(self):
        client, fn = _setup_add()
        await fn(issue_id="PROJ-1", duration_minutes=30, work_type="251-9")
        assert _sent_type(client) == {"id": "251-9"}
        client.get.assert_not_called()

    async def test_digit_leading_name_is_not_mistaken_for_an_id(self):
        """The regression a loose heuristic introduces: "1-on-1" is a NAME.

        `work_type[0].isdigit() and "-" in work_type` sends it as an id and
        the log is rejected or filed under the wrong type.
        """
        client, fn = _setup_add()
        await fn(issue_id="PROJ-1", duration_minutes=30, work_type="1-on-1")
        assert _sent_type(client) == {"id": "251-11"}

    @pytest.mark.parametrize("name", ["24-7 oncall", "2-factor rollout", "3-way merge"])
    async def test_other_digit_leading_names_are_looked_up_not_sent_as_ids(self, name):
        client, fn = _setup_add(types=[{"id": "251-99", "name": name}])
        await fn(issue_id="PROJ-1", duration_minutes=30, work_type=name)
        assert _sent_type(client) == {"id": "251-99"}

    async def test_unknown_name_lists_the_valid_ones(self):
        client, fn = _setup_add()
        with pytest.raises(ValueError, match="Unknown work type") as exc:
            await fn(issue_id="PROJ-1", duration_minutes=30, work_type="Bikeshedding")
        # the message must be actionable, not just a rejection
        assert "Development" in str(exc.value) and "Review" in str(exc.value)
        client.post.assert_not_called()

    async def test_no_admin_rights_falls_back_to_name(self):
        from yt_mcp.errors import YouTrackPermissionError
        client, fn = _setup_add(get_raises=YouTrackPermissionError("403"))
        await fn(issue_id="PROJ-1", duration_minutes=30, work_type="Development")
        assert _sent_type(client) == {"name": "Development"}

    async def test_empty_catalogue_falls_back_to_name(self):
        client, fn = _setup_add(types=[])
        await fn(issue_id="PROJ-1", duration_minutes=30, work_type="Development")
        assert _sent_type(client) == {"name": "Development"}

    async def test_no_work_type_omits_type_and_skips_lookup(self):
        client, fn = _setup_add()
        await fn(issue_id="PROJ-1", duration_minutes=30)
        assert _sent_type(client) is None
        client.get.assert_not_called()

    async def test_blank_work_type_is_treated_as_absent(self):
        client, fn = _setup_add()
        await fn(issue_id="PROJ-1", duration_minutes=30, work_type="   ")
        assert _sent_type(client) is None
        client.get.assert_not_called()

    async def test_surrounding_whitespace_on_an_id_still_resolves(self):
        client, fn = _setup_add()
        await fn(issue_id="PROJ-1", duration_minutes=30, work_type="  251-9  ")
        assert _sent_type(client) == {"id": "251-9"}
        client.get.assert_not_called()

    @pytest.mark.parametrize("bad", [{"error": "denied"}, "forbidden", 42])
    async def test_non_list_catalogue_falls_back_instead_of_crashing(self, bad):
        """A refusal can arrive as a dict or a string; iterating it yields
        values with no .get and used to raise AttributeError."""
        client, fn = _setup_add(types=bad)
        await fn(issue_id="PROJ-1", duration_minutes=30, work_type="Development")
        assert _sent_type(client) == {"name": "Development"}
