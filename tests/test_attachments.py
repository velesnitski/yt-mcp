"""Tests for add_attachment — dual-mode upload (file_path / inline content)."""

import base64

import pytest
from unittest.mock import AsyncMock, MagicMock

from mcp.server.fastmcp import FastMCP

from yt_mcp.resolver import InstanceResolver
from yt_mcp.tools.attachments import register as _register, _guess_mime, _full_url


def _make_mcp(post_result=None, base_url="https://acme.youtrack.cloud"):
    mcp = FastMCP("test")
    client = MagicMock()
    client.base_url = base_url
    client.post_multipart = AsyncMock(
        return_value=post_result if post_result is not None
        else {"id": "1-1", "name": "x", "size": 0, "url": "/attachments/1-1"}
    )
    resolver = MagicMock(spec=InstanceResolver)
    resolver.resolve = MagicMock(return_value=client)
    _register(mcp, resolver)
    return mcp, client


def _fn(mcp, name):
    return mcp._tool_manager._tools[name].fn


def _multipart_of(client):
    """(path, files_dict, params) of the last post_multipart call."""
    call = client.post_multipart.call_args
    path = call.args[0] if call.args else call.kwargs["path"]
    files = call.kwargs.get("files")
    params = call.kwargs.get("params")
    return path, files, params


# --- pure helpers ----------------------------------------------------------

class TestGuessMime:
    def test_known_extensions(self):
        assert _guess_mime("report.html", default="x") == "text/html"
        assert _guess_mime("data.csv", default="x") == "text/csv"
        assert _guess_mime("pic.png", default="x") == "image/png"

    def test_unknown_uses_default(self):
        assert _guess_mime("noext", default="application/octet-stream") == "application/octet-stream"


class TestFullUrl:
    def test_relative_gets_base(self):
        c = MagicMock(); c.base_url = "https://acme.youtrack.cloud/"
        assert _full_url(c, "/attachments/1") == "https://acme.youtrack.cloud/attachments/1"

    def test_absolute_untouched(self):
        c = MagicMock(); c.base_url = "https://acme.youtrack.cloud"
        assert _full_url(c, "https://cdn/x") == "https://cdn/x"

    def test_empty(self):
        c = MagicMock(); c.base_url = "https://acme.youtrack.cloud"
        assert _full_url(c, "") == ""


# --- input validation ------------------------------------------------------

class TestValidation:
    @pytest.mark.asyncio
    async def test_no_input(self):
        mcp, client = _make_mcp()
        out = await _fn(mcp, "add_attachment")(issue_id="PROJ-1")
        assert "Provide either" in out
        client.post_multipart.assert_not_called()

    @pytest.mark.asyncio
    async def test_both_inputs(self):
        mcp, client = _make_mcp()
        out = await _fn(mcp, "add_attachment")(
            issue_id="PROJ-1", file_path="/x", content="y", filename="y.txt"
        )
        assert "only one" in out
        client.post_multipart.assert_not_called()

    @pytest.mark.asyncio
    async def test_content_without_filename(self):
        mcp, client = _make_mcp()
        out = await _fn(mcp, "add_attachment")(issue_id="PROJ-1", content="hello")
        assert "filename` is required" in out
        client.post_multipart.assert_not_called()

    @pytest.mark.asyncio
    async def test_file_not_found(self, monkeypatch, tmp_path):
        # not-found is checked AFTER confinement — allowlist the root so we
        # reach the isfile check rather than the secure-default refusal.
        monkeypatch.setenv("YOUTRACK_ATTACHMENT_ROOTS", str(tmp_path))
        mcp, client = _make_mcp()
        out = await _fn(mcp, "add_attachment")(
            issue_id="PROJ-1", file_path=str(tmp_path / "nope.txt"))
        assert "File not found" in out
        client.post_multipart.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_base64(self):
        mcp, client = _make_mcp()
        out = await _fn(mcp, "add_attachment")(
            issue_id="PROJ-1", content="!!!not base64!!!", filename="x.bin", content_base64=True
        )
        assert "not valid base64" in out
        client.post_multipart.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_content_refused(self):
        mcp, client = _make_mcp()
        out = await _fn(mcp, "add_attachment")(issue_id="PROJ-1", content="", filename="x.txt")
        # empty content falls into the "provide either" guard first
        assert "Provide either" in out


# --- inline content mode ---------------------------------------------------

class TestInlineContent:
    @pytest.mark.asyncio
    async def test_text_utf8_and_mime(self):
        mcp, client = _make_mcp()
        await _fn(mcp, "add_attachment")(
            issue_id="PROJ-1", content="# Report\n\nhello", filename="report.md"
        )
        path, files, params = _multipart_of(client)
        assert path == "/api/issues/PROJ-1/attachments"
        name, data, mime = files["file"]
        assert name == "report.md"
        assert data == b"# Report\n\nhello"
        assert mime in ("text/markdown", "text/plain")  # md may not be in mimetypes db
        assert params["fields"] == "id,name,size,url"

    @pytest.mark.asyncio
    async def test_base64_binary_decoded(self):
        raw = b"\x89PNG\r\n\x1a\n binary"
        b64 = base64.b64encode(raw).decode()
        mcp, client = _make_mcp()
        await _fn(mcp, "add_attachment")(
            issue_id="PROJ-1", content=b64, filename="chart.png", content_base64=True
        )
        _, files, _ = _multipart_of(client)
        name, data, mime = files["file"]
        assert data == raw
        assert mime == "image/png"

    @pytest.mark.asyncio
    async def test_mime_override(self):
        mcp, client = _make_mcp()
        await _fn(mcp, "add_attachment")(
            issue_id="PROJ-1", content="x", filename="weird", mime_type="application/x-custom"
        )
        _, files, _ = _multipart_of(client)
        assert files["file"][2] == "application/x-custom"

    @pytest.mark.asyncio
    async def test_url_taken_present_in_returns(self):
        mcp, client = _make_mcp(
            post_result={"id": "1-2", "name": "report.md", "size": 14, "url": "/attachments/1-2"}
        )
        out = await _fn(mcp, "add_attachment")(
            issue_id="PROJ-1", content="hello", filename="report.md"
        )
        assert "✓ Attached" in out
        assert "report.md" in out
        assert "https://acme.youtrack.cloud/attachments/1-2" in out


# --- file_path mode --------------------------------------------------------

class TestFilePath:
    @pytest.fixture(autouse=True)
    def _allow_tmp(self, monkeypatch, tmp_path):
        # file_path mode is secure-by-default (ADR-027); allowlist this test's
        # tmp dir so the legitimate-upload behaviour can be exercised.
        monkeypatch.setenv("YOUTRACK_ATTACHMENT_ROOTS", str(tmp_path))

    @pytest.mark.asyncio
    async def test_reads_disk_file(self, tmp_path):
        p = tmp_path / "data.csv"
        p.write_bytes(b"a,b,c\n1,2,3\n")
        mcp, client = _make_mcp()
        await _fn(mcp, "add_attachment")(issue_id="PROJ-1", file_path=str(p))
        _, files, _ = _multipart_of(client)
        name, data, mime = files["file"]
        assert name == "data.csv"
        assert data == b"a,b,c\n1,2,3\n"
        assert mime == "text/csv"

    @pytest.mark.asyncio
    async def test_filename_override(self, tmp_path):
        p = tmp_path / "tmp123.bin"
        p.write_bytes(b"\x00\x01\x02")
        mcp, client = _make_mcp()
        await _fn(mcp, "add_attachment")(
            issue_id="PROJ-1", file_path=str(p), filename="renamed.dat"
        )
        _, files, _ = _multipart_of(client)
        assert files["file"][0] == "renamed.dat"

    @pytest.mark.asyncio
    async def test_youtrack_returns_list(self, tmp_path):
        # YT sometimes returns a list of created attachments.
        p = tmp_path / "f.txt"
        p.write_text("hi")
        mcp, client = _make_mcp(post_result=[{"id": "1-9", "name": "f.txt", "size": 2, "url": "/a/9"}])
        out = await _fn(mcp, "add_attachment")(issue_id="PROJ-1", file_path=str(p))
        assert "f.txt" in out
        assert "https://acme.youtrack.cloud/a/9" in out

    @pytest.mark.asyncio
    async def test_url_parsed_from_issue_url(self, tmp_path):
        p = tmp_path / "f.txt"
        p.write_text("hi")
        mcp, client = _make_mcp()
        await _fn(mcp, "add_attachment")(
            issue_id="https://acme.youtrack.cloud/issue/PROJ-7/slug", file_path=str(p)
        )
        path, _, _ = _multipart_of(client)
        assert path == "/api/issues/PROJ-7/attachments"


# --- security: file_path confinement (ADR-027, CWE-22/CWE-73) ---------------

import os


class TestFilePathConfinement:
    """`file_path` must be confined to YOUTRACK_ATTACHMENT_ROOTS; the reported
    arbitrary-file-read + exfil path must be closed. No sensitive real files
    are touched — everything runs against a tmp allowlisted root."""

    @pytest.mark.asyncio
    async def test_disabled_by_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv("YOUTRACK_ATTACHMENT_ROOTS", raising=False)
        secret = tmp_path / "secret.txt"
        secret.write_text("top secret")
        mcp, client = _make_mcp()
        out = await _fn(mcp, "add_attachment")(issue_id="DEMO-1", file_path=str(secret))
        assert "disabled" in out.lower()
        client.post_multipart.assert_not_called()  # nothing exfiltrated

    @pytest.mark.asyncio
    async def test_absolute_escape_refused(self, monkeypatch, tmp_path):
        root = tmp_path / "root"; root.mkdir()
        outside = tmp_path / "outside.txt"; outside.write_text("nope")
        monkeypatch.setenv("YOUTRACK_ATTACHMENT_ROOTS", str(root))
        mcp, client = _make_mcp()
        out = await _fn(mcp, "add_attachment")(issue_id="DEMO-1", file_path=str(outside))
        assert "Refused" in out
        client.post_multipart.assert_not_called()

    @pytest.mark.asyncio
    async def test_traversal_refused(self, monkeypatch, tmp_path):
        root = tmp_path / "root"; root.mkdir()
        (tmp_path / "outside.txt").write_text("nope")
        monkeypatch.setenv("YOUTRACK_ATTACHMENT_ROOTS", str(root))
        mcp, client = _make_mcp()
        out = await _fn(mcp, "add_attachment")(
            issue_id="DEMO-1", file_path=str(root / ".." / "outside.txt"))
        assert "Refused" in out
        client.post_multipart.assert_not_called()

    @pytest.mark.asyncio
    async def test_symlink_escape_refused(self, monkeypatch, tmp_path):
        root = tmp_path / "root"; root.mkdir()
        outside = tmp_path / "outside.txt"; outside.write_text("nope")
        link = root / "innocent.txt"
        os.symlink(outside, link)  # symlink INSIDE root, points OUTSIDE
        monkeypatch.setenv("YOUTRACK_ATTACHMENT_ROOTS", str(root))
        mcp, client = _make_mcp()
        out = await _fn(mcp, "add_attachment")(issue_id="DEMO-1", file_path=str(link))
        assert "Refused" in out
        client.post_multipart.assert_not_called()

    @pytest.mark.asyncio
    async def test_sibling_prefix_not_confused(self, monkeypatch, tmp_path):
        root = tmp_path / "root"; root.mkdir()
        evil = tmp_path / "root-evil"; evil.mkdir()
        (evil / "x.txt").write_text("nope")
        monkeypatch.setenv("YOUTRACK_ATTACHMENT_ROOTS", str(root))
        mcp, client = _make_mcp()
        out = await _fn(mcp, "add_attachment")(issue_id="DEMO-1", file_path=str(evil / "x.txt"))
        assert "Refused" in out  # commonpath, not str.startswith

    @pytest.mark.asyncio
    async def test_in_root_allowed(self, monkeypatch, tmp_path):
        root = tmp_path / "root"; root.mkdir()
        f = root / "report.txt"; f.write_bytes(b"legit content")
        monkeypatch.setenv("YOUTRACK_ATTACHMENT_ROOTS", str(root))
        mcp, client = _make_mcp()
        out = await _fn(mcp, "add_attachment")(issue_id="DEMO-1", file_path=str(f))
        assert "Attached" in out
        _, files, _ = _multipart_of(client)
        assert files["file"][1] == b"legit content"  # the confined file uploaded

    @pytest.mark.asyncio
    async def test_content_mode_unaffected(self, monkeypatch):
        monkeypatch.delenv("YOUTRACK_ATTACHMENT_ROOTS", raising=False)
        mcp, client = _make_mcp()
        out = await _fn(mcp, "add_attachment")(
            issue_id="DEMO-1", content="hello", filename="note.txt")
        assert "Attached" in out
        _, files, _ = _multipart_of(client)
        assert files["file"][1] == b"hello"
