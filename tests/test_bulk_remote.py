"""Remote bulk flow: upload page, background jobs, isolation between users, resume, cleanup."""
import importlib
import io
import json
import sqlite3
import sys
import zipfile

import pytest
from starlette.testclient import TestClient

import bulk
import server
from fakes import FakeHeuApi, make_bundle, no_sleep

pytestmark = pytest.mark.anyio

BASE = {"source_document_id": "tpl1", "email_subject": "Contratto", "email_body": "Firma qui"}
CSV = "Nome;Email;Indirizzo\n" + "".join(f"Persona {i};p{i}@x.it;Via {i}\n" for i in range(12)) + "Errata;non-email;Via\n"


@pytest.fixture(scope="module")
def rs(tmp_path_factory):
    root = tmp_path_factory.mktemp("remote")
    mp = pytest.MonkeyPatch()
    mp.setenv("HEU_MCP_SECRET_KEY", "wZ9kLpQxT3vN8mR2sY6bF1hJ4dG7cA0eV5uI9oK2nXs=")
    mp.setenv("HEU_MCP_DB", str(root / "heu.db"))
    mp.setenv("PUBLIC_URL", "https://mcp.example.test")
    mp.setenv("HEU_BULK_WORKER", "0")
    mp.delenv("BUCKET_NAME", raising=False)
    local_backend = server._bulk_backend
    sys.modules.pop("remote_server", None)
    module = importlib.import_module("remote_server")
    module.init_db()
    module.bulk_backend.init_db()
    yield module
    server.set_remote_mode(False)
    server.set_bulk_backend(local_backend)
    mp.undo()


@pytest.fixture
def api(rs):
    fake = FakeHeuApi()
    rs.bulk_backend.request = fake
    rs.bulk_backend.throttle = bulk.Throttle(0, sleep=no_sleep)
    server.set_remote_mode(True)
    server.set_bulk_backend(rs.bulk_backend)
    return fake


def grant(rs, grant_id: str, key: str = "heu-key") -> str:
    with rs._db() as c:
        c.execute("INSERT OR IGNORE INTO grants (grant_id, client_id, heu_key_enc, scope, created_at)"
                  " VALUES (?,?,?,?,0)", (grant_id, "c", rs._encrypt(key), "heu:read"))
    return grant_id


async def as_user(grant_id: str, name: str, **arguments):
    tok = server.set_principal(grant_id)
    try:
        res = await server.call_tool(name, arguments)
    finally:
        server._principal_var.reset(tok)
    return json.loads(res[0].text)


def upload(rs, link: str, content: bytes, filename="lista.csv"):
    path = link.replace("https://mcp.example.test", "")
    client = TestClient(rs.app)
    assert client.get(path).status_code == 200
    return client.post(path, files={"file": (filename, content)})


async def test_full_send_flow_and_isolation(rs, api):
    alice, bob = grant(rs, "g-alice"), grant(rs, "g-bob")
    link = await as_user(alice, "get_bulk_upload_link")
    up = link["upload_id"]

    early = await as_user(alice, "create_pdf_documents_bulk", upload_id=up, **BASE)
    assert early["error"] and "non è ancora stato caricato" in early["body"]

    page = upload(rs, link["link_caricamento"], CSV.encode())
    assert page.status_code == 200 and "13 righe" in page.text
    again = upload(rs, link["link_caricamento"], b"Nome;Email\nX;x@x.it\n")
    assert "già caricato" in again.text

    # Another user cannot use, or even see, Alice's upload.
    stolen = await as_user(bob, "create_pdf_documents_bulk", upload_id=up, **BASE)
    assert stolen["error"] and "non trovato" in stolen["body"]

    dry = await as_user(alice, "create_pdf_documents_bulk", upload_id=up, **BASE)
    assert dry["righe_valide"] == 12 and dry["righe_con_errori"] == 1 and api.created == []
    assert "Persona" not in json.dumps(dry)

    job = await as_user(alice, "create_pdf_documents_bulk", upload_id=up, dry_run=False, **BASE)
    assert job["stato"] == "in_coda" and job["da_inviare"] == 12
    same = await as_user(alice, "create_pdf_documents_bulk", upload_id=up, dry_run=False, **BASE)
    assert same["job_id"] == job["job_id"] and "già stato messo in invio" in same["nota"]

    assert await rs.bulk_backend.run_next() is True
    assert len(api.created) == 12

    status = await as_user(alice, "get_bulk_job_status", job_id=job["job_id"])
    assert status["stato"] == "completato" and status["inviate"] == 12 and status["errori"] == 1
    assert status["errori_principali"] == ["riga 14: email di 'Cliente' non valida (non-email)"]
    assert status["file"] == "lista.esito.csv"

    assert (await as_user(bob, "get_bulk_job_status", job_id=job["job_id"]))["error"]

    client = TestClient(rs.app)
    resp = client.get(status["link_download"].replace("https://mcp.example.test", ""))
    assert resp.status_code == 200
    outcome = bulk.parse_table(resp.content, "lista.esito.csv")
    assert sum(bool(r["stato"] == "inviato" and r["document_id"]) for r in outcome.rows) == 12

    # Re-uploading the outcome file (or the original) never sends twice.
    link2 = await as_user(alice, "get_bulk_upload_link")
    upload(rs, link2["link_caricamento"], resp.content, "lista.esito.csv")
    redo = await as_user(alice, "create_pdf_documents_bulk", upload_id=link2["upload_id"], **BASE)
    assert redo["gia_inviate"] == 12 and redo["righe_valide"] == 0


async def test_row_data_is_encrypted_at_rest(rs, api):
    alice = grant(rs, "g-enc")
    link = await as_user(alice, "get_bulk_upload_link")
    upload(rs, link["link_caricamento"], CSV.encode())
    await as_user(alice, "create_pdf_documents_bulk", upload_id=link["upload_id"], dry_run=False, **BASE)
    raw = sqlite3.connect(rs.DB_PATH).execute("SELECT group_concat(data_enc) FROM bulk_rows").fetchone()[0]
    assert "p3@x.it" not in raw
    with rs._db() as c:
        key = c.execute("SELECT object_key FROM bulk_uploads WHERE upload_id = ?", (link["upload_id"],)).fetchone()[0]
    stored = rs.DB_PATH.rsplit("/", 1)[0] + "/bulk/" + key
    assert b"p3@x.it" not in open(stored, "rb").read()
    await rs.bulk_backend.run_next()


async def test_restart_mid_send_does_not_resend(rs, api):
    alice = grant(rs, "g-crash")
    link = await as_user(alice, "get_bulk_upload_link")
    upload(rs, link["link_caricamento"], b"Nome;Email;Indirizzo\nA;a@x.it;V\nB;b@x.it;V\nC;c@x.it;V\n")
    job = await as_user(alice, "create_pdf_documents_bulk", upload_id=link["upload_id"], dry_run=False, **BASE)
    # Simulate a crash: row A was being sent and the API did create it; row B's fate is unknown.
    api.documents.append({"id": "900", "name": "Contratto servizi - A", "created_at": "2099-01-01T00:00:00Z"})
    with rs._db() as c:
        c.execute("UPDATE bulk_jobs SET state = 'in_corso', started_at = 0 WHERE job_id = ?", (job["job_id"],))
        c.execute("UPDATE bulk_rows SET state = 'in_corso' WHERE job_id = ? AND idx IN (0, 1)", (job["job_id"],))
    api.documents.append({"id": "901", "name": "Contratto servizi - B", "created_at": "2099-01-01T00:00:00Z"})
    api.documents.append({"id": "902", "name": "Contratto servizi - B", "created_at": "2099-01-01T00:00:00Z"})

    rs.bulk_backend.recover()
    await rs.bulk_backend.run_next()
    status = await as_user(alice, "get_bulk_job_status", job_id=job["job_id"])
    assert [p["document_name"] for p in api.created] == ["Contratto servizi - C"]
    assert status["inviate"] == 2 and status["da_verificare"] == 1  # B: two candidates, left for a human


async def test_revoked_key_stops_job_and_keeps_outcome(rs, api):
    alice = grant(rs, "g-stop")
    link = await as_user(alice, "get_bulk_upload_link")
    upload(rs, link["link_caricamento"], b"Nome;Email;Indirizzo\nA;a@x.it;V\nB;b@x.it;V\n")
    job = await as_user(alice, "create_pdf_documents_bulk", upload_id=link["upload_id"], dry_run=False, **BASE)
    api.create_script = [(401, {"message": "unauthorized"}, {})]
    await rs.bulk_backend.run_next()
    status = await as_user(alice, "get_bulk_job_status", job_id=job["job_id"])
    assert status["stato"] == "interrotto" and "API key" in status["nota"]
    assert status["ancora_da_inviare"] == 1 and "link_download" in status


async def test_bulk_download_job_builds_archive(rs, api):
    alice = grant(rs, "g-dl")
    api.documents = [
        {"id": "1", "name": "NDA Rossi", "status": "signed", "tags": ["Fornitori"], "created_at": "2026-10-05T10:00:00Z"},
        {"id": "2", "name": "NDA Bianchi", "status": "signed", "tags": ["Fornitori"], "created_at": "2026-10-06T10:00:00Z"},
    ]
    api.bundles = {"1": make_bundle()}  # document 2: caller is not a recipient
    job = await as_user(alice, "download_pdf_bundles_bulk", folder="fornitori", include_reading_copy=True)
    assert job["documenti_selezionati"] == 2 and job["stato"] == "in_coda"
    assert "destination_folder" not in json.dumps(job)
    await rs.bulk_backend.run_next()
    status = await as_user(alice, "get_bulk_job_status", job_id=job["job_id"])
    assert status["stato"] == "completato" and status["scaricati"] == 1 and status["errori"] == 1
    client = TestClient(rs.app)
    archive = client.get(status["link_download"].replace("https://mcp.example.test", "")).content
    names = sorted(zipfile.ZipFile(io.BytesIO(archive)).namelist())
    assert names == ["LEGGIMI.txt", "NDA Rossi - 1.zip",
                     "copie di consultazione/NDA Rossi - 1 (copia con audit trail).pdf"]
    assert zipfile.ZipFile(io.BytesIO(archive)).read("NDA Rossi - 1.zip") == api.bundles["1"]


async def test_upload_page_rejects_bad_files_and_unknown_links(rs, api):
    alice = grant(rs, "g-bad")
    link = await as_user(alice, "get_bulk_upload_link")
    bad = upload(rs, link["link_caricamento"], b"%PDF-1.4", "contratto.pdf")
    assert bad.status_code == 400 and "Formato non supportato" in bad.text
    client = TestClient(rs.app)
    assert client.get("/bulk/upload/inventato").status_code == 404
    assert client.get("/bulk/files/inventato").status_code == 404


async def test_cleanup_removes_expired_data(rs, api):
    alice = grant(rs, "g-old")
    link = await as_user(alice, "get_bulk_upload_link")
    upload(rs, link["link_caricamento"], CSV.encode())
    job = await as_user(alice, "create_pdf_documents_bulk", upload_id=link["upload_id"], dry_run=False, **BASE)
    await rs.bulk_backend.run_next()
    with rs._db() as c:
        c.execute("UPDATE bulk_jobs SET expires_at = 1 WHERE job_id = ?", (job["job_id"],))
        c.execute("UPDATE bulk_uploads SET expires_at = 1 WHERE upload_id = ?", (link["upload_id"],))
    assert rs.bulk_backend.cleanup() >= 2
    with rs._db() as c:
        assert c.execute("SELECT COUNT(*) FROM bulk_rows WHERE job_id = ?", (job["job_id"],)).fetchone()[0] == 0
    root = rs.DB_PATH.rsplit("/", 1)[0] + "/bulk"
    import os
    assert not os.path.exists(f"{root}/jobs/{job['job_id']}")
    assert not os.path.exists(f"{root}/uploads/{link['upload_id']}")
    assert (await as_user(alice, "get_bulk_job_status", job_id=job["job_id"]))["error"]


async def test_oauth_connection_reaches_bulk_tools_over_http(rs, api, monkeypatch):
    """Full path as Claude uses it: OAuth, then tools over the MCP HTTP endpoint."""
    import base64
    import hashlib
    import secrets
    from urllib.parse import parse_qs, urlparse

    async def accept(key):
        return (True, "") if key == "VALID" else (False, "no")

    monkeypatch.setattr(rs, "_validate_heu_key", accept)
    redirect = "https://claude.ai/api/mcp/auth_callback"
    with TestClient(rs.app) as client:  # runs the lifespan once for this module
        cid = client.post("/register", json={"redirect_uris": [redirect], "client_name": "Claude"}).json()["client_id"]
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        form = {"client_id": cid, "redirect_uri": redirect, "code_challenge": challenge, "state": "s",
                "scope": "heu:read heu:write"}
        location = client.post("/authorize", data={**form, "api_key": "VALID"}, follow_redirects=False).headers["location"]
        code = parse_qs(urlparse(location).query)["code"][0]
        access = client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                             "code_verifier": verifier, "client_id": cid,
                                             "redirect_uri": redirect}).json()["access_token"]
        headers = {"Authorization": f"Bearer {access}", "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json"}

        def rpc(method, params):
            r = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            body = r.text
            if "data:" in body:
                body = [line[6:] for line in body.splitlines() if line.startswith("data: ")][0]
            return json.loads(body)["result"]

        names = {t["name"] for t in rpc("tools/list", {})["tools"]}
        assert {"create_pdf_documents_bulk", "get_bulk_upload_link", "get_bulk_job_status"} <= names
        assert "download_pdf_bundle" not in names

        result = rpc("tools/call", {"name": "get_bulk_upload_link", "arguments": {}})
        link = json.loads(result["content"][0]["text"])
        assert link["link_caricamento"].startswith("https://mcp.example.test/bulk/upload/")
        with rs._db() as c:
            owner = c.execute("SELECT grant_id FROM bulk_uploads WHERE upload_id = ?", (link["upload_id"],)).fetchone()
            granted = c.execute("SELECT grant_id FROM grants ORDER BY created_at DESC LIMIT 1").fetchone()
        assert owner["grant_id"] == granted["grant_id"]
