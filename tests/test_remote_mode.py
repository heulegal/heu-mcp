"""Remote (multi-tenant) mode must never touch the server's filesystem on a caller's behalf."""
import json

import pytest

import server
from conftest import make_pdf

pytestmark = pytest.mark.anyio


async def test_local_mode_exposes_filesystem_tools():
    names = {t.name for t in await server.list_tools()}
    assert server.LOCAL_FS_TOOLS <= names


async def test_remote_mode_hides_filesystem_tools(remote_mode):
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert not (server.LOCAL_FS_TOOLS & names)
    locate = next(t for t in tools if t.name == "locate_pdf_text")
    assert "file_path" not in locate.inputSchema["properties"]
    assert "document_id" in locate.inputSchema["properties"]


async def test_remote_mode_does_not_mutate_local_schemas(remote_mode):
    await server.list_tools()
    server.set_remote_mode(False)
    locate = next(t for t in await server.list_tools() if t.name == "locate_pdf_text")
    assert "file_path" in locate.inputSchema["properties"]


async def test_cross_tenant_read_is_blocked(remote_mode, tmp_path):
    # Another tenant's contract left on the shared disk by a previous download.
    leaked = tmp_path / "heu_pdf_123.pdf"
    leaked.write_bytes(make_pdf([("Contratto riservato cliente A", 100, 700)]))
    res = await server.call_tool(
        "locate_pdf_text", {"file_path": str(leaked), "include_all_lines": True}
    )
    out = json.loads(res[0].text)
    assert out["error"] is True and out["status"] == 403
    assert "riservato" not in res[0].text


async def test_server_state_overwrite_is_blocked(remote_mode, tmp_path, monkeypatch):
    db = tmp_path / "heu-mcp.db"
    db.write_bytes(b"precious")

    async def fake_request(*a, **k):
        return 200, b"%PDF-1.4 fake", {}

    monkeypatch.setattr(server, "_request", fake_request)
    for tool in sorted(server.LOCAL_FS_TOOLS):
        res = await server.call_tool(tool, {"document_id": "1", "output_path": str(db),
                                            "file_path": str(db)})
        assert json.loads(res[0].text)["status"] == 403, tool
    assert db.read_bytes() == b"precious"


async def test_locate_by_document_id_still_works_remotely(remote_mode, monkeypatch):
    pdf = make_pdf([("Firma del Cliente", 100, 100)])

    async def fake_request(method, path, **k):
        assert path == "/pdfs/42/download"
        return 200, pdf, {}

    monkeypatch.setattr(server, "_request", fake_request)
    res = await server.call_tool("locate_pdf_text", {"document_id": "42"})
    out = json.loads(res[0].text)
    assert out["total_matches"] == 1
