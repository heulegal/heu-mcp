"""Remote (multi-tenant) backend for bulk operations.

Data never travels through the conversation:

* the user uploads the CSV/XLSX through a personal, short-lived link served by
  this server (``/bulk/upload/<token>``);
* sends and downloads run as background jobs, one at a time, so a large job
  cannot exceed the HEU rate limit that all users of this server share;
* outcome files and ZIP archives are fetched through capability links
  (``/bulk/files/<token>``) that expire with the job.

Every upload, job and link is bound to the OAuth grant that created it and is
deleted after ``RETENTION`` seconds (24 hours). Row data at rest in SQLite is
encrypted with the server's Fernet key; uploaded files are encrypted before
they reach object storage.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import json
import logging
import os
import secrets
import tempfile
import time
import zipfile
from typing import Any, Callable

import anyio
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

import bulk
import bulk_tools
import server as heu

log = logging.getLogger("heu.bulk")

RETENTION = int(os.getenv("HEU_BULK_RETENTION", str(24 * 3600)))
UPLOAD_LINK_TTL = 30 * 60
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_DOWNLOAD_DOCUMENTS = 1000
REMOTE_MIN_INTERVAL = float(os.getenv("HEU_BULK_MIN_INTERVAL_REMOTE", "1.2"))
CLEANUP_EVERY = 600

STATE_QUEUED = "in_coda"
STATE_RUNNING = "in_corso"
STATE_DONE = "completato"
STATE_STOPPED = "interrotto"
STATE_FAILED = "errore"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bulk_uploads (
    upload_id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    filename TEXT,
    object_key TEXT,
    rows INTEGER,
    created_at INTEGER NOT NULL,
    link_expires_at INTEGER NOT NULL,
    uploaded_at INTEGER,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bulk_jobs (
    job_id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    params TEXT NOT NULL,
    upload_id TEXT,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    expires_at INTEGER NOT NULL,
    progress TEXT,
    message TEXT,
    result_key TEXT,
    result_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_bulk_jobs_state ON bulk_jobs(state, created_at);
CREATE TABLE IF NOT EXISTS bulk_rows (
    job_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    number INTEGER NOT NULL,
    data_enc TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL,
    document_id TEXT,
    error TEXT,
    PRIMARY KEY (job_id, idx)
);
CREATE TABLE IF NOT EXISTS bulk_links (
    token_hash TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
"""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> int:
    return int(time.time())


class RemoteBulkBackend:
    """Bulk backend for the shared remote server.

    ``db`` returns a sqlite3 connection (row_factory = Row), ``encrypt``/``decrypt``
    are the server's Fernet helpers, ``key_for_grant`` returns the decrypted HEU
    API key of a grant (None if revoked), ``storage`` is a storage.* backend.
    """

    def __init__(self, db: Callable, storage, public_url: str, encrypt: Callable[[str], str],
                 decrypt: Callable[[str], str], key_for_grant: Callable[[str], str | None],
                 request: Callable | None = None, throttle: bulk.Throttle | None = None):
        self.db = db
        self.storage = storage
        self.public_url = public_url.rstrip("/")
        self.encrypt = encrypt
        self.decrypt = decrypt
        self.key_for_grant = key_for_grant
        self.request = request or (lambda *a, **k: heu._request(*a, **k))
        self.throttle = throttle or bulk.Throttle(REMOTE_MIN_INTERVAL)
        self._wake = asyncio.Event()

    def init_db(self) -> None:
        with self.db() as c:
            c.executescript(SCHEMA)

    # ------------------------------------------------------------------ helpers

    def _grant(self) -> str:
        grant = heu.get_principal()
        if not grant:
            raise bulk.BulkError("Utente non identificato: ricollega il connettore HEU.")
        return grant

    async def _store(self, fn, *args):
        return await anyio.to_thread.run_sync(fn, *args)

    def _job(self, job_id: str, grant: str):
        with self.db() as c:
            row = c.execute("SELECT * FROM bulk_jobs WHERE job_id = ? AND grant_id = ?", (job_id, grant)).fetchone()
        if not row or row["expires_at"] < _now():
            raise bulk.BulkError("Lavoro non trovato o scaduto (i lavori restano disponibili 24 ore).")
        return row

    def _new_link(self, job_id: str, expires_at: int) -> str:
        token = secrets.token_urlsafe(32)
        with self.db() as c:
            c.execute("INSERT INTO bulk_links (token_hash, job_id, expires_at) VALUES (?,?,?)",
                      (_hash(token), job_id, expires_at))
        return f"{self.public_url}/bulk/files/{token}"

    def _queue_position(self, job_id: str) -> int:
        with self.db() as c:
            row = c.execute("SELECT created_at FROM bulk_jobs WHERE job_id = ?", (job_id,)).fetchone()
            ahead = c.execute(
                "SELECT COUNT(*) FROM bulk_jobs WHERE state IN (?, ?) AND created_at <= ? AND job_id != ?",
                (STATE_QUEUED, STATE_RUNNING, row["created_at"], job_id),
            ).fetchone()[0]
        return ahead

    # --------------------------------------------------------------- tool calls

    async def handle(self, name: str, arguments: dict) -> dict:
        if name == bulk_tools.UPLOAD_LINK:
            return self.upload_link()
        if name == bulk_tools.CREATE:
            return await self.create(arguments)
        if name == bulk_tools.DOWNLOAD:
            return await self.download(arguments)
        if name == bulk_tools.JOB_STATUS:
            return self.status(str(arguments.get("job_id") or ""))
        raise bulk.BulkError(f"Strumento sconosciuto: {name}")

    def upload_link(self) -> dict:
        grant = self._grant()
        token = secrets.token_urlsafe(32)
        upload_id = f"up_{secrets.token_urlsafe(9)}"
        now = _now()
        with self.db() as c:
            c.execute(
                "INSERT INTO bulk_uploads (upload_id, grant_id, token_hash, created_at, link_expires_at, expires_at)"
                " VALUES (?,?,?,?,?,?)",
                (upload_id, grant, _hash(token), now, now + UPLOAD_LINK_TTL, now + RETENTION),
            )
        return {
            "upload_id": upload_id,
            "link_caricamento": f"{self.public_url}/bulk/upload/{token}",
            "valido_per_minuti": UPLOAD_LINK_TTL // 60,
            "istruzioni": "Dai il link all'utente: carica il file .csv o .xlsx e poi conferma qui. "
                          "Poi usa upload_id con create_pdf_documents_bulk (prima con dry_run=true).",
        }

    async def _load_upload(self, upload_id: str, grant: str) -> tuple[bytes, str]:
        with self.db() as c:
            row = c.execute("SELECT * FROM bulk_uploads WHERE upload_id = ? AND grant_id = ?",
                            (upload_id, grant)).fetchone()
        if not row or row["expires_at"] < _now():
            raise bulk.BulkError("upload_id non trovato o scaduto: chiedi un nuovo link con get_bulk_upload_link.")
        if not row["uploaded_at"]:
            raise bulk.BulkError("Il file non è ancora stato caricato: chiedi all'utente di aprire il link e caricarlo.")
        encrypted = await self._store(self.storage.get_bytes, row["object_key"])
        data = self.decrypt(encrypted.decode()).encode("latin-1")
        return data, row["filename"]

    def _previously_sent(self, grant: str) -> dict[str, str]:
        """Fingerprints already sent by this user in jobs still within retention."""
        with self.db() as c:
            rows = c.execute(
                "SELECT r.fingerprint, r.document_id FROM bulk_rows r JOIN bulk_jobs j ON j.job_id = r.job_id"
                " WHERE j.grant_id = ? AND j.expires_at > ? AND r.state = ?",
                (grant, _now(), bulk.STATE_SENT),
            ).fetchall()
        return {r["fingerprint"]: r["document_id"] or "" for r in rows}

    async def create(self, arguments: dict) -> dict:
        grant = self._grant()
        upload_id = str(arguments.get("upload_id") or "")
        if not upload_id:
            raise bulk.BulkError("Serve upload_id: chiama prima get_bulk_upload_link e fai caricare il file all'utente.")
        dry_run = arguments.get("dry_run", True) is not False

        if not dry_run:
            # The same upload is sent at most once: a second confirmation returns the existing job.
            with self.db() as c:
                existing = c.execute(
                    "SELECT job_id FROM bulk_jobs WHERE upload_id = ? AND grant_id = ? AND kind = 'create'"
                    " AND state != ?", (upload_id, grant, STATE_FAILED),
                ).fetchone()
            if existing:
                out = self.status(existing["job_id"])
                out["nota"] = "Questo file è già stato messo in invio: nessun nuovo invio creato."
                return out

        data, filename = await self._load_upload(upload_id, grant)
        try:
            plan = await bulk_tools.prepare_plan(self.request, self.throttle, arguments, data, filename)
        except bulk_tools.MappingProblem as e:
            return e.summary()

        sent_before = self._previously_sent(grant)
        for r in plan.rows:
            if r.state == bulk.STATE_PENDING and r.fingerprint in sent_before:
                r.state, r.document_id = bulk.STATE_SENT, sent_before[r.fingerprint]

        if dry_run:
            return bulk.summarize(plan, dry_run=True)

        job_id = f"job_{secrets.token_urlsafe(9)}"
        now = _now()
        params = {
            "source_document_id": str(arguments["source_document_id"]),
            "email_subject": arguments["email_subject"],
            "email_body": arguments["email_body"],
            "signature_type": arguments.get("signature_type"),
            "column_mapping": arguments.get("column_mapping"),
            "headers": plan.table.headers,
            "fmt": plan.table.fmt,
            "delimiter": plan.table.delimiter,
            "outcome_name": bulk.outcome_filename(filename),
        }
        with self.db() as c:
            c.execute(
                "INSERT INTO bulk_jobs (job_id, grant_id, kind, state, params, upload_id, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (job_id, grant, "create", STATE_QUEUED, json.dumps(params), upload_id, now, now + RETENTION),
            )
            c.executemany(
                "INSERT INTO bulk_rows (job_id, idx, number, data_enc, fingerprint, state, document_id, error)"
                " VALUES (?,?,?,?,?,?,?,?)",
                [(job_id, i, r.number, self.encrypt(json.dumps(r.data, ensure_ascii=False)), r.fingerprint,
                  r.state, r.document_id, r.error) for i, r in enumerate(plan.rows)],
            )
        self._wake.set()
        counts = plan.counts()
        return {
            "job_id": job_id,
            "stato": STATE_QUEUED,
            "da_inviare": counts.get(bulk.STATE_PENDING, 0),
            "saltate_con_errori": counts.get(bulk.STATE_INVALID, 0),
            "gia_inviate": counts.get(bulk.STATE_SENT, 0),
            "lavori_davanti_in_coda": self._queue_position(job_id),
            "nota": "Invio avviato in background. Usa get_bulk_job_status per l'avanzamento e il file di esito.",
        }

    async def download(self, arguments: dict) -> dict:
        grant = self._grant()
        flt = bulk_tools.download_filter(arguments)
        documents = await bulk_tools.list_all_documents(self.request, self.throttle)
        selected, missing = bulk.select_documents(documents, flt)
        summary: dict[str, Any] = {"documenti_selezionati": len(selected)}
        if missing:
            summary["id_non_trovati"] = missing[: bulk.MAX_ERRORS_LISTED]
        if arguments.get("dry_run") or not selected:
            return summary
        if len(selected) > MAX_DOWNLOAD_DOCUMENTS:
            raise bulk.BulkError(
                f"{len(selected)} documenti selezionati: il massimo per un archivio è {MAX_DOWNLOAD_DOCUMENTS}. "
                "Restringi il filtro (per esempio per intervallo di date)."
            )
        job_id = f"job_{secrets.token_urlsafe(9)}"
        now = _now()
        params = {
            "documents": [{"id": str(d.get("id")), "name": d.get("name") or ""} for d in selected],
            "with_copy": bool(arguments.get("include_reading_copy")),
        }
        with self.db() as c:
            c.execute(
                "INSERT INTO bulk_jobs (job_id, grant_id, kind, state, params, created_at, expires_at, progress)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (job_id, grant, "download", STATE_QUEUED, json.dumps(params), now, now + RETENTION,
                 json.dumps({"totale": len(selected), "fatti": 0, "errori": []})),
            )
        self._wake.set()
        summary.update({
            "job_id": job_id,
            "stato": STATE_QUEUED,
            "lavori_davanti_in_coda": self._queue_position(job_id),
            "nota": "Archivio in preparazione in background. Usa get_bulk_job_status per il link di download.",
        })
        return summary

    def status(self, job_id: str) -> dict:
        grant = self._grant()
        job = self._job(job_id, grant)
        out: dict[str, Any] = {"job_id": job_id, "tipo": "invio" if job["kind"] == "create" else "download",
                               "stato": job["state"]}
        if job["state"] == STATE_QUEUED:
            out["lavori_davanti_in_coda"] = self._queue_position(job_id)
        if job["kind"] == "create":
            with self.db() as c:
                counts = dict(c.execute(
                    "SELECT state, COUNT(*) FROM bulk_rows WHERE job_id = ? GROUP BY state", (job_id,)).fetchall())
                bad = c.execute(
                    "SELECT number, error FROM bulk_rows WHERE job_id = ? AND state IN (?,?,?) ORDER BY idx LIMIT ?",
                    (job_id, bulk.STATE_FAILED, bulk.STATE_INVALID, bulk.STATE_TO_VERIFY, bulk.MAX_ERRORS_LISTED),
                ).fetchall()
            out.update({
                "righe_totali": sum(counts.values()),
                "inviate": counts.get(bulk.STATE_SENT, 0),
                "errori": counts.get(bulk.STATE_FAILED, 0) + counts.get(bulk.STATE_INVALID, 0),
                "da_verificare": counts.get(bulk.STATE_TO_VERIFY, 0),
                "ancora_da_inviare": counts.get(bulk.STATE_PENDING, 0) + counts.get(bulk.STATE_IN_PROGRESS, 0),
            })
            if bad:
                out["errori_principali"] = [f"riga {b['number']}: {b['error']}" for b in bad]
        else:
            progress = json.loads(job["progress"] or "{}")
            out.update({"documenti": progress.get("totale", 0), "scaricati": progress.get("fatti", 0)})
            if progress.get("errori"):
                out["errori"] = len(progress["errori"])
                out["errori_principali"] = progress["errori"][: bulk.MAX_ERRORS_LISTED]
        if job["message"]:
            out["nota"] = job["message"]
        if job["result_key"]:
            out["link_download"] = self._new_link(job_id, job["expires_at"])
            out["file"] = job["result_name"]
            out["disponibile_fino_a"] = time.strftime("%d/%m/%Y %H:%M UTC", time.gmtime(job["expires_at"]))
        return out

    # ------------------------------------------------------------------- worker

    def recover(self) -> None:
        """After a restart: requeue jobs that were running and flag rows caught mid-request."""
        with self.db() as c:
            c.execute("UPDATE bulk_jobs SET state = ? WHERE state = ?", (STATE_QUEUED, STATE_RUNNING))
            c.execute(
                "UPDATE bulk_rows SET state = ?, error = ? WHERE state = ?",
                (bulk.STATE_TO_VERIFY, "invio interrotto: verificare se il documento è stato creato",
                 bulk.STATE_IN_PROGRESS),
            )

    def _next_job(self):
        with self.db() as c:
            return c.execute(
                "SELECT * FROM bulk_jobs WHERE state = ? AND expires_at > ? ORDER BY created_at LIMIT 1",
                (STATE_QUEUED, _now()),
            ).fetchone()

    def _finish(self, job_id: str, state: str, message: str | None = None, result_key: str | None = None,
                result_name: str | None = None) -> None:
        with self.db() as c:
            c.execute(
                "UPDATE bulk_jobs SET state = ?, finished_at = ?, message = COALESCE(?, message),"
                " result_key = COALESCE(?, result_key), result_name = COALESCE(?, result_name) WHERE job_id = ?",
                (state, _now(), message, result_key, result_name, job_id),
            )

    async def run_next(self) -> bool:
        """Run the oldest queued job, if any. Returns False when the queue is empty."""
        job = self._next_job()
        if not job:
            return False
        job_id = job["job_id"]
        with self.db() as c:
            c.execute("UPDATE bulk_jobs SET state = ?, started_at = COALESCE(started_at, ?) WHERE job_id = ?",
                      (STATE_RUNNING, _now(), job_id))
        api_key = await anyio.to_thread.run_sync(self.key_for_grant, job["grant_id"])
        if not api_key:
            self._finish(job_id, STATE_FAILED, "Autorizzazione revocata: ricollega il connettore e rilancia.")
            return True
        key_token = heu.set_api_key(api_key)
        try:
            if job["kind"] == "create":
                await self._run_create(job)
            else:
                await self._run_download(job)
        except bulk.StopJob as e:
            self._finish(job_id, STATE_STOPPED, str(e))
        except Exception as e:  # never let one job kill the worker
            log.exception("bulk job %s failed", job_id)
            self._finish(job_id, STATE_FAILED, f"Errore interno: {type(e).__name__}. Ricarica il file di esito per riprendere.")
        finally:
            heu._api_key_var.reset(key_token)
        return True

    def _load_plan(self, job, template: bulk.Template, mapping: bulk.Mapping, params: dict) -> bulk.Plan:
        with self.db() as c:
            rows = c.execute("SELECT * FROM bulk_rows WHERE job_id = ? ORDER BY idx", (job["job_id"],)).fetchall()
        states = []
        for r in rows:
            data = json.loads(self.decrypt(r["data_enc"]))
            states.append(bulk.RowState(number=r["number"], data=data, fingerprint=r["fingerprint"], state=r["state"],
                                        document_id=r["document_id"] or "", error=r["error"] or ""))
        table = bulk.Table(headers=params["headers"], rows=[s.data for s in states], fmt=params["fmt"],
                           delimiter=params["delimiter"])
        return bulk.Plan(template=template, mapping=mapping, rows=states, table=table)

    async def _run_create(self, job) -> None:
        job_id = job["job_id"]
        params = json.loads(job["params"])
        template = await bulk.fetch_template(self.request, params["source_document_id"])
        mapping = bulk.build_mapping(params["headers"], template, params.get("column_mapping"))
        if mapping.problems:
            self._finish(job_id, STATE_FAILED, "Il template è cambiato dopo la verifica: " + "; ".join(mapping.problems))
            return
        plan = self._load_plan(job, template, mapping, params)
        index = {id(r): i for i, r in enumerate(plan.rows)}

        await bulk.reconcile(plan, self.request, self.throttle, since=(job["started_at"] or job["created_at"]))

        def save(r: bulk.RowState) -> None:
            with self.db() as c:
                c.execute("UPDATE bulk_rows SET state = ?, document_id = ?, error = ? WHERE job_id = ? AND idx = ?",
                          (r.state, r.document_id, r.error, job_id, index[id(r)]))

        for r in plan.rows:
            if r.state == bulk.STATE_SENT and r.document_id:
                save(r)

        async def on_change(r: bulk.RowState) -> None:
            save(r)

        stopped: str | None = None
        try:
            await bulk.send_rows(plan, self.request, self.throttle, params["email_subject"], params["email_body"],
                                 params.get("signature_type"), on_change)
        except bulk.StopJob as e:
            stopped = str(e)

        key = f"jobs/{job_id}/{params['outcome_name']}"
        await self._store(self.storage.put_bytes, key, plan.outcome_bytes())
        if stopped:
            self._finish(job_id, STATE_STOPPED, stopped + " Ricarica il file di esito per riprendere.", key,
                         params["outcome_name"])
        else:
            self._finish(job_id, STATE_DONE, None, key, params["outcome_name"])

    async def _run_download(self, job) -> None:
        job_id = job["job_id"]
        params = json.loads(job["params"])
        documents = params["documents"]
        progress = {"totale": len(documents), "fatti": 0, "errori": []}

        def save_progress():
            with self.db() as c:
                c.execute("UPDATE bulk_jobs SET progress = ? WHERE job_id = ?", (json.dumps(progress), job_id))

        name = f"contratti_firmati_{time.strftime('%Y%m%d')}_{job_id[-6:]}.zip"
        with tempfile.TemporaryDirectory(prefix="heu-bulk-") as tmp:
            path = os.path.join(tmp, "archive.zip")
            used: set[str] = set()
            with zipfile.ZipFile(path, "w", allowZip64=True) as zf:
                for doc in documents:
                    base = bulk.readable_name(doc)
                    while base in used:
                        base += "_"
                    used.add(base)
                    got = await bulk.fetch_bundle(self.request, self.throttle, doc, params["with_copy"])
                    if got.bundle:
                        # Original bundle stored untouched: it carries the legal value.
                        zf.writestr(f"{base}.zip", got.bundle, compress_type=zipfile.ZIP_STORED)
                        progress["fatti"] += 1
                    if got.copy:
                        zf.writestr(f"copie di consultazione/{base} (copia con audit trail).pdf", got.copy,
                                    compress_type=zipfile.ZIP_DEFLATED)
                    if got.error and len(progress["errori"]) < 200:
                        progress["errori"].append(f"{base}: {got.error}")
                    save_progress()
                if params["with_copy"]:
                    zf.writestr(
                        "LEGGIMI.txt",
                        "Ogni file .zip è il bundle originale scaricato da HEU (documento firmato, audit trail, "
                        "artefatti FES): è quello con valore legale.\nLe copie nella cartella 'copie di "
                        "consultazione' uniscono documento e audit trail in un solo PDF per comodità di lettura: "
                        "riscrivere il PDF invalida la firma digitale incorporata.\n",
                    )
            if progress["fatti"] == 0:
                self._finish(job_id, STATE_FAILED, "Nessun bundle scaricato: vedi gli errori.")
                return
            key = f"jobs/{job_id}/{name}"
            await self._store(self.storage.put_file, key, path)
        self._finish(job_id, STATE_DONE, None, key, name)

    def cleanup(self) -> int:
        """Delete expired uploads, jobs, rows, links and their stored files."""
        now = _now()
        with self.db() as c:
            uploads = [r["upload_id"] for r in c.execute("SELECT upload_id FROM bulk_uploads WHERE expires_at < ?", (now,))]
            jobs = [r["job_id"] for r in c.execute("SELECT job_id FROM bulk_jobs WHERE expires_at < ?", (now,))]
        for upload_id in uploads:
            self.storage.delete_prefix(f"uploads/{upload_id}")
        for job_id in jobs:
            self.storage.delete_prefix(f"jobs/{job_id}")
        with self.db() as c:
            c.executemany("DELETE FROM bulk_uploads WHERE upload_id = ?", [(u,) for u in uploads])
            c.executemany("DELETE FROM bulk_rows WHERE job_id = ?", [(j,) for j in jobs])
            c.executemany("DELETE FROM bulk_jobs WHERE job_id = ?", [(j,) for j in jobs])
            c.execute("DELETE FROM bulk_links WHERE expires_at < ?", (now,))
        return len(uploads) + len(jobs)

    async def run_forever(self) -> None:
        self.recover()
        last_cleanup = 0.0
        while True:
            try:
                if time.monotonic() - last_cleanup > CLEANUP_EVERY:
                    await anyio.to_thread.run_sync(self.cleanup)
                    last_cleanup = time.monotonic()
                if await self.run_next():
                    continue
            except Exception:
                log.exception("bulk worker loop error")
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=30)

    # --------------------------------------------------------------- web pages

    def _upload_row(self, token: str):
        with self.db() as c:
            return c.execute("SELECT * FROM bulk_uploads WHERE token_hash = ?", (_hash(token),)).fetchone()

    async def upload_page(self, request: Request) -> Response:
        token = request.path_params["token"]
        row = self._upload_row(token)
        now = _now()
        if not row or row["link_expires_at"] < now:
            return _page("Link non valido o scaduto", "Chiedi a Claude un nuovo link di caricamento.", 404)
        if row["uploaded_at"]:
            return _page("File già caricato", f"Il file <strong>{html.escape(row['filename'] or '')}</strong> "
                         f"({row['rows']} righe) è già stato ricevuto. Torna su Claude per proseguire.")
        if request.method == "GET":
            return _page("Carica il file per l'invio in blocco", UPLOAD_FORM, 200)

        if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES + 64 * 1024:
            return _page("File troppo grande", "Il limite è 10 MB.", 413)
        form = await request.form(max_files=1, max_fields=5)
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return _page("Nessun file", "Seleziona un file .csv o .xlsx.", 400)
        filename = os.path.basename(upload.filename or "file")
        data = await upload.read(MAX_UPLOAD_BYTES + 1)
        await upload.close()
        if len(data) > MAX_UPLOAD_BYTES:
            return _page("File troppo grande", "Il limite è 10 MB.", 413)
        try:
            table = await anyio.to_thread.run_sync(bulk.parse_table, data, filename)
        except bulk.BulkError as e:
            return _page("File non leggibile", html.escape(str(e)), 400)

        key = f"uploads/{row['upload_id']}/{secrets.token_hex(8)}"
        encrypted = self.encrypt(data.decode("latin-1")).encode()
        await self._store(self.storage.put_bytes, key, encrypted)
        with self.db() as c:
            won = c.execute(
                "UPDATE bulk_uploads SET filename = ?, object_key = ?, rows = ?, uploaded_at = ?"
                " WHERE upload_id = ? AND uploaded_at IS NULL",
                (filename, key, len(table.rows), now, row["upload_id"]),
            ).rowcount
        if not won:  # a concurrent upload on the same link got there first
            await self._store(self.storage.delete_prefix, key)
            return _page("File già caricato", "Questo link è già stato usato. Torna su Claude per proseguire.")
        return _page("File ricevuto",
                     f"<strong>{html.escape(filename)}</strong>: {len(table.rows)} righe, "
                     f"{len(table.headers)} colonne.<br><br>Ora torna su Claude e scrivi <em>fatto</em>: "
                     "prima di inviare qualsiasi cosa ti mostrerà una verifica delle righe.")

    async def file_download(self, request: Request) -> Response:
        token = request.path_params["token"]
        with self.db() as c:
            link = c.execute("SELECT * FROM bulk_links WHERE token_hash = ?", (_hash(token),)).fetchone()
            job = c.execute("SELECT * FROM bulk_jobs WHERE job_id = ?", (link["job_id"],)).fetchone() if link else None
        now = _now()
        if not link or link["expires_at"] < now or not job or not job["result_key"]:
            return _page("Link non valido o scaduto", "I file restano disponibili 24 ore. Chiedi a Claude lo stato del lavoro.", 404)
        return await self._store(self.storage.response, job["result_key"], job["result_name"], link["expires_at"] - now)


UPLOAD_FORM = """
<p>Carica il file <strong>.csv</strong> o <strong>.xlsx</strong> con una riga per ogni documento da inviare.
I dati arrivano direttamente al server HEU, senza passare dalla conversazione, e vengono cancellati
automaticamente dopo 24 ore.</p>
<form method="post" enctype="multipart/form-data">
  <label for="file">File (massimo 10 MB)</label>
  <input id="file" name="file" type="file" accept=".csv,.xlsx" required>
  <button type="submit">Carica</button>
</form>
<p class="small">Niente viene inviato ai firmatari a questo punto: prima Claude ti mostrerà una verifica delle righe
e ti chiederà conferma.</p>
"""

PAGE = """<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>HEU Legal · __TITLE__</title>
<style>
 :root{color-scheme:light dark;--bg:#f5f5f7;--card:#fff;--fg:#1d1d1f;--muted:#6e6e73;--accent:#5600FD;--line:#d2d2d7}
 @media (prefers-color-scheme:dark){:root{--bg:#1a1a1c;--card:#252528;--fg:#f5f5f7;--muted:#a1a1a6;--accent:#8c5cff;--line:#3a3a3d}}
 body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;min-height:100vh;
   display:flex;align-items:center;justify-content:center;background:var(--bg);color:var(--fg)}
 .card{background:var(--card);padding:2.25rem;border-radius:16px;max-width:480px;width:calc(100% - 2rem);
   box-shadow:0 4px 24px rgba(0,0,0,.08);box-sizing:border-box}
 h1{font-size:1.3rem;margin:0 0 1rem}
 p{line-height:1.55;font-size:.95rem}
 .small{font-size:.82rem;color:var(--muted)}
 label{display:block;font-weight:600;font-size:.88rem;margin:1.25rem 0 .5rem}
 input[type=file]{width:100%;padding:.6rem;border:1px dashed var(--line);border-radius:9px;box-sizing:border-box}
 button{margin-top:1.1rem;width:100%;padding:.8rem;font-size:.95rem;font-weight:600;color:#fff;
   background:var(--accent);border:0;border-radius:9px;cursor:pointer}
</style></head>
<body><main class="card"><h1>__TITLE__</h1>__BODY__</main></body></html>"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    content = PAGE.replace("__TITLE__", html.escape(title)).replace("__BODY__", body if "<" in body else f"<p>{body}</p>")
    return HTMLResponse(content, status_code=status, headers={
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
        "X-Frame-Options": "DENY",
    })
