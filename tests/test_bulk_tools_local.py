"""Bulk tools through server.call_tool in local (stdio) mode, with the API mocked."""
import json

import pytest

import bulk
import bulk_tools
import server
from fakes import FakeHeuApi, make_bundle, no_sleep

pytestmark = pytest.mark.anyio


@pytest.fixture
def api(monkeypatch):
    fake = FakeHeuApi()
    monkeypatch.setattr(server, "_request", fake)
    backend = bulk_tools.LocalBulkBackend(request=lambda *a, **k: server._request(*a, **k),
                                          min_interval=0, sleep=no_sleep)
    monkeypatch.setattr(server, "_bulk_backend", backend)
    return fake


async def call(name, **arguments):
    res = await server.call_tool(name, arguments)
    return json.loads(res[0].text)


def write_csv(tmp_path, rows: int, bad_rows=()) -> str:
    lines = ["Nome;Email;Indirizzo"]
    for i in range(rows):
        email = "non-valida" if i in bad_rows else f"p{i}@x.it"
        lines.append(f"Persona {i};{email};Via {i}")
    path = tmp_path / "invio.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


BASE = {"source_document_id": "tpl1", "email_subject": "Contratto", "email_body": "Firma qui"}


async def test_dry_run_sends_nothing_and_replies_short(api, tmp_path):
    path = write_csv(tmp_path, 400, bad_rows={10, 20})
    out = await call("create_pdf_documents_bulk", file_path=path, **BASE)
    assert out["righe_totali"] == 400 and out["righe_valide"] == 398 and out["righe_con_errori"] == 2
    assert api.created == []
    assert out["mappatura"]["campi"] == {"Indirizzo": "Indirizzo"}
    # The reply never carries the rows themselves.
    assert "Persona 5" not in json.dumps(out)
    assert (tmp_path / "invio.esito.csv").exists()


async def test_send_writes_outcome_and_resume_has_no_duplicates(api, tmp_path, monkeypatch):
    path = write_csv(tmp_path, 30, bad_rows={3})
    # Simulate an interruption: the time budget runs out after a few rows.
    clock = iter(range(0, 10_000))
    monkeypatch.setattr(bulk.time, "monotonic", lambda: float(next(clock)))
    server._bulk_backend.time_budget = 25
    first = await call("create_pdf_documents_bulk", file_path=path, dry_run=False, **BASE)
    assert first["stato"] == "parziale"
    sent_first = len(api.created)
    assert 0 < sent_first < 29

    server._bulk_backend.time_budget = 10_000
    second = await call("create_pdf_documents_bulk", file_path=path, dry_run=False, **BASE)
    assert second["stato"] == "completato"
    assert second["inviate"] == 29 and second["errori"] == 1
    names = [p["document_name"] for p in api.created]
    assert len(names) == len(set(names)) == 29

    outcome = bulk.parse_table((tmp_path / "invio.esito.csv").read_bytes(), "invio.esito.csv")
    assert outcome.headers[-3:] == ["document_id", "stato", "errore"]
    assert sum(r["stato"] == "inviato" for r in outcome.rows) == 29
    assert all(r["document_id"] for r in outcome.rows if r["stato"] == "inviato")

    third = await call("create_pdf_documents_bulk", file_path=path, dry_run=False, **BASE)
    assert third["inviate"] == 29 and len(api.created) == 29


async def test_mapping_problem_stops_before_sending(api, tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("Cliente;Posta\nMario;m@x.it\n", encoding="utf-8")
    out = await call("create_pdf_documents_bulk", file_path=str(path), dry_run=False, **BASE)
    assert "Mappatura" in out["errore"] and out["colonne_del_file"] == ["Cliente", "Posta"]
    assert api.created == []


async def test_upload_id_is_refused_locally(api):
    out = await call("create_pdf_documents_bulk", upload_id="u1", **BASE)
    assert out["error"] is True and "file_path" in out["body"]


async def test_bulk_download_skips_existing_and_reports_only_counts(api, tmp_path):
    api.documents = [
        {"id": "1", "name": "NDA Rossi", "status": "signed", "tags": ["Fornitori"], "created_at": "2026-10-05T10:00:00Z"},
        {"id": "2", "name": "NDA Bianchi", "status": "signed", "tags": ["Fornitori"], "created_at": "2026-10-06T10:00:00Z"},
        {"id": "3", "name": "Altro", "status": "signed", "tags": ["Clienti"], "created_at": "2026-10-06T10:00:00Z"},
    ]
    api.bundles = {"1": make_bundle(), "2": make_bundle()}
    dest = tmp_path / "firmati"

    preview = await call("download_pdf_bundles_bulk", folder="Fornitori", dry_run=True)
    assert preview == {"documenti_selezionati": 2}

    out = await call("download_pdf_bundles_bulk", folder="Fornitori", destination_folder=str(dest),
                     include_reading_copy=True)
    assert out["scaricati"] == 2 and out["stato"] == "completato"
    assert sorted(p.name for p in dest.iterdir()) == [
        "NDA Bianchi - 2 (copia con audit trail).pdf", "NDA Bianchi - 2.zip",
        "NDA Rossi - 1 (copia con audit trail).pdf", "NDA Rossi - 1.zip",
    ]
    again = await call("download_pdf_bundles_bulk", folder="Fornitori", destination_folder=str(dest),
                       include_reading_copy=True)
    assert again["scaricati"] == 0 and again["gia_presenti"] == 2


async def test_local_mode_hides_remote_only_tools():
    tools = {t.name: t for t in await server.list_tools()}
    assert "get_bulk_upload_link" not in tools and "get_bulk_job_status" not in tools
    assert "upload_id" not in tools["create_pdf_documents_bulk"].inputSchema["properties"]
    assert "file_path" in tools["create_pdf_documents_bulk"].inputSchema["properties"]


async def test_remote_mode_view_of_bulk_tools(remote_mode):
    tools = {t.name: t for t in await server.list_tools()}
    assert {"get_bulk_upload_link", "get_bulk_job_status"} <= set(tools)
    create = tools["create_pdf_documents_bulk"]
    assert "file_path" not in create.inputSchema["properties"]
    assert "upload_id" in create.inputSchema["properties"]
    assert "get_bulk_upload_link" in create.description
    assert "destination_folder" not in tools["download_pdf_bundles_bulk"].inputSchema["properties"]
    refused = await server.call_tool("create_pdf_documents_bulk", {"file_path": "/etc/passwd", **BASE})
    assert json.loads(refused[0].text)["status"] == 403


# ------------------------------------------------------------ compact replies

FULL_PDF = {"message": "ok", "status": 201, "data": {
    "id": "88", "name": "NDA", "status": "to_sign", "signature_type": "fes", "type": "document",
    "created_at": "x", "updated_at": "y", "archived_at": None,
    "signers": [{"id": "9", "full_name": "Mario", "email": "m@x.it", "has_read": False, "has_signed": False}],
}}


async def test_create_pdf_document_reply_is_compact_unless_verbose(monkeypatch):
    async def fake(method, path, **k):
        return 201, FULL_PDF, {}

    monkeypatch.setattr(server, "_request", fake)
    args = {"source_document_id": "1", "email_subject": "s", "email_body": "b",
            "signers": [{"source_id": "1", "full_name": "Mario", "email": "m@x.it"}]}
    short = await call("create_pdf_document", **args)
    assert short == {"id": "88", "name": "NDA", "status": "to_sign", "signature_type": "fes",
                     "signers": [{"id": "9", "full_name": "Mario", "email": "m@x.it"}]}
    full = await call("create_pdf_document", verbose=True, **args)
    assert full == FULL_PDF


def test_template_compaction_counts_placeholders():
    body = {"data": {"id": "5", "name": "T", "type": "template", "signers": [{"id": "1", "full_name": "A"}],
                     "placeholders": [{"type": "signature"}, {"type": "text"}, {"type": "text"}]}}
    assert server._compact_template(body)["placeholders"] == {"totale": 3, "per_tipo": {"signature": 1, "text": 2}}
    assert server._compact_pdf_document({"unexpected": True}) == {"unexpected": True}
