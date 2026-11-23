"""MCP tool definitions for bulk operations, plus the local (stdio) backend.

The remote backend lives in ``bulk_remote.py`` and implements the same
``handle(name, arguments)`` contract. ``server.py`` picks one or the other
depending on the mode it runs in.

Tool descriptions are Italian (they are read by the end user's assistant);
code and comments are English.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp.types import Tool, ToolAnnotations

import bulk

MAX_INPUT_BYTES = 20 * 1024 * 1024
LOCAL_TIME_BUDGET = float(os.getenv("HEU_BULK_TIME_BUDGET", "240"))
MIN_INTERVAL = float(os.getenv("HEU_BULK_MIN_INTERVAL", "1.1"))

CREATE = "create_pdf_documents_bulk"
DOWNLOAD = "download_pdf_bundles_bulk"
UPLOAD_LINK = "get_bulk_upload_link"
JOB_STATUS = "get_bulk_job_status"
NAMES = frozenset({CREATE, DOWNLOAD, UPLOAD_LINK, JOB_STATUS})

# Mode-specific surface, consumed by server.py.
REMOTE_ONLY_TOOLS = frozenset({UPLOAD_LINK, JOB_STATUS})
LOCAL_ONLY_ARGUMENTS = {CREATE: ("file_path",), DOWNLOAD: ("destination_folder",)}
REMOTE_ONLY_ARGUMENTS = {CREATE: ("upload_id",)}


# ------------------------------------------------------------------ descriptions

_CREATE_COMMON = (
    "Una riga = un documento. Usalo SEMPRE al posto di chiamare create_pdf_document più volte: "
    "i dati delle righe non passano dalla conversazione e la risposta contiene solo conteggi. "
    "Esegui prima con dry_run=true (default): verifica tutte le righe (email, campi obbligatori "
    "del template, limiti, duplicati) senza inviare nulla e mostra la mappatura colonne → firmatari "
    "e campi, dedotta dalle intestazioni (correggibile con column_mapping). "
    "IMPORTANTE: mostra il riepilogo all'utente e chiedi conferma esplicita prima di dry_run=false. "
    "Le righe con errori vengono saltate, le altre inviate; un errore non ferma l'invio. "
)
CREATE_LOCAL_DESCRIPTION = (
    "Invia in firma lo stesso template PDF a molti destinatari leggendo un file .csv o .xlsx dal "
    "computer dell'utente (file_path). " + _CREATE_COMMON +
    "Con dry_run=false invia riga per riga rispettando i limiti dell'API e scrive accanto al file "
    "un file di esito (stesse righe + document_id, stato, errore). Se la risposta dice 'parziale', "
    "richiamalo con gli stessi parametri: riprende dal file di esito saltando le righe già inviate, "
    "senza doppi invii."
)
CREATE_REMOTE_DESCRIPTION = (
    "Invia in firma lo stesso template PDF a molti destinatari leggendo un file .csv o .xlsx "
    "caricato dall'utente: prima chiama get_bulk_upload_link, dai il link all'utente, attendi che "
    "confermi il caricamento, poi usa upload_id. Non chiedere all'utente di incollare i dati in chat. "
    + _CREATE_COMMON +
    "Con dry_run=false l'invio parte in background sul server e la risposta contiene un job_id: "
    "usa get_bulk_job_status per l'avanzamento e, a fine lavoro, per il link al file di esito "
    "(stesse righe + document_id, stato, errore). Per riprendere o correggere righe in errore "
    "l'utente può ricaricare il file di esito: le righe già inviate vengono saltate."
)

_DOWNLOAD_COMMON = (
    "Seleziona i documenti PDF con un elenco di document_id oppure con filtri: cartella (i tag di HEU), "
    "stato (default 'signed', 'any' per tutti), intervallo di date di creazione, testo nel nome. "
    "Per ogni documento scarica il bundle originale (PDF firmato + audit trail + artefatti FES) con "
    "nome leggibile 'nome documento - id'. Con include_reading_copy=true aggiunge una copia unica "
    "in PDF (documento seguito dall'audit trail): è solo una copia di consultazione, perché "
    "riscrivere il PDF invalida la firma digitale incorporata; il valore legale resta nel bundle. "
    "La risposta contiene solo conteggi. Con dry_run=true conta i documenti selezionati senza scaricare. "
)
DOWNLOAD_LOCAL_DESCRIPTION = (
    "Scarica in blocco i bundle firmati in una cartella del computer dell'utente (destination_folder). "
    + _DOWNLOAD_COMMON +
    "I file già presenti vengono saltati; se la risposta dice 'parziale', richiamalo con gli stessi "
    "parametri per continuare."
)
DOWNLOAD_REMOTE_DESCRIPTION = (
    "Prepara sul server un archivio ZIP con i bundle firmati di molti documenti. " + _DOWNLOAD_COMMON +
    "Il lavoro parte in background e la risposta contiene un job_id: usa get_bulk_job_status per "
    "l'avanzamento e, a fine lavoro, per il link di download (valido 24 ore)."
)

COLUMN_MAPPING_SCHEMA = {
    "type": "object",
    "description": (
        "Correzioni alla mappatura automatica. Formato: "
        '{"firmatari": {"<nome firmatario nel template>": {"nome": "Colonna" | ["Nome", "Cognome"], '
        '"email": "Colonna"}}, "campi": {"<etichetta campo>": "Colonna"}, '
        '"nome_documento": "Colonna" oppure "Contratto {Colonna}"}. '
        "Default nome documento: '<nome template> - <primo firmatario>'."
    ),
}


def tool_definitions() -> list[Tool]:
    return [
        Tool(
            name=CREATE,
            description=CREATE_LOCAL_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "source_document_id": {"type": "string", "description": "ID del template PDF"},
                    "file_path": {"type": "string", "description": "Percorso del file .csv o .xlsx sul computer dell'utente"},
                    "upload_id": {"type": "string", "description": "ID restituito da get_bulk_upload_link, dopo che l'utente ha caricato il file"},
                    "email_subject": {"type": "string", "description": "Oggetto dell'email di invito alla firma"},
                    "email_body": {"type": "string", "description": "Testo dell'email di invito alla firma"},
                    "signature_type": {"type": "string", "enum": ["fes", "fea"], "description": "Tipo firma. Default: fes"},
                    "column_mapping": COLUMN_MAPPING_SCHEMA,
                    "dry_run": {"type": "boolean", "description": "true (default): solo verifica, nessun invio. false: invia."},
                },
                "required": ["source_document_id", "email_subject", "email_body"],
            },
            annotations=ToolAnnotations(
                title="Bulk Send PDFs for Signature", readOnlyHint=False, destructiveHint=False,
                idempotentHint=True, openWorldHint=True,
            ),
        ),
        Tool(
            name=DOWNLOAD,
            description=DOWNLOAD_LOCAL_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "document_ids": {"type": "array", "items": {"type": "string"}, "description": "Elenco esplicito di ID PDF (alternativo ai filtri)"},
                    "folder": {"type": "string", "description": "Cartella HEU (tag) dei documenti"},
                    "status": {"type": "string", "enum": ["signed", "to_sign", "in_progress", "any"], "description": "Stato dei documenti. Default: signed"},
                    "created_from": {"type": "string", "description": "Creati da questa data (AAAA-MM-GG)"},
                    "created_to": {"type": "string", "description": "Creati fino a questa data inclusa (AAAA-MM-GG)"},
                    "name_contains": {"type": "string", "description": "Testo contenuto nel nome del documento"},
                    "include_reading_copy": {"type": "boolean", "description": "Aggiunge una copia unica PDF + audit trail per ogni documento. Default: false"},
                    "destination_folder": {"type": "string", "description": "Cartella del computer dell'utente dove salvare i file"},
                    "dry_run": {"type": "boolean", "description": "true: conta i documenti selezionati senza scaricarli. Default: false"},
                },
            },
            annotations=ToolAnnotations(
                title="Bulk Download Signed Bundles", readOnlyHint=True, destructiveHint=False,
                idempotentHint=True, openWorldHint=True,
            ),
        ),
        Tool(
            name=UPLOAD_LINK,
            description=(
                "Crea un link personale e temporaneo (30 minuti) con cui l'utente carica un file .csv o "
                ".xlsx per create_pdf_documents_bulk, senza che i dati passino dalla conversazione. "
                "Dai il link all'utente e attendi che confermi il caricamento prima di proseguire."
            ),
            inputSchema={"type": "object", "properties": {}},
            annotations=ToolAnnotations(
                title="Get Bulk Upload Link", readOnlyHint=False, destructiveHint=False,
                idempotentHint=False, openWorldHint=False,
            ),
        ),
        Tool(
            name=JOB_STATUS,
            description=(
                "Stato di un lavoro in blocco (invio o download) avviato sul server: avanzamento, "
                "errori principali e, a lavoro finito, il link al file di esito o all'archivio ZIP. "
                "I file restano disponibili per 24 ore."
            ),
            inputSchema={
                "type": "object",
                "properties": {"job_id": {"type": "string", "description": "ID del lavoro"}},
                "required": ["job_id"],
            },
            annotations=ToolAnnotations(
                title="Get Bulk Job Status", readOnlyHint=True, destructiveHint=False,
                idempotentHint=True, openWorldHint=False,
            ),
        ),
    ]


REMOTE_DESCRIPTIONS = {CREATE: CREATE_REMOTE_DESCRIPTION, DOWNLOAD: DOWNLOAD_REMOTE_DESCRIPTION}


def download_filter(arguments: dict) -> bulk.DownloadFilter:
    ids = arguments.get("document_ids") or []
    if isinstance(ids, str):
        ids = [i.strip() for i in ids.split(",")]
    flt = bulk.DownloadFilter(
        document_ids=[str(i) for i in ids if str(i).strip()],
        folder=arguments.get("folder") or None,
        status=arguments.get("status") or "signed",
        created_from=arguments.get("created_from") or None,
        created_to=arguments.get("created_to") or None,
        name_contains=arguments.get("name_contains") or None,
    )
    if not flt.document_ids and not any([flt.folder, flt.created_from, flt.created_to, flt.name_contains]):
        if flt.status in (None, "any"):
            raise bulk.BulkError("Indica almeno un filtro (cartella, date, nome, stato) o un elenco di document_id.")
    return flt


async def list_all_documents(request, throttle: bulk.Throttle) -> list[dict]:
    status, body, _ = await bulk.call_api(request, "GET", "/pdfs", throttle, params={"type": "document"})
    if status != 200:
        raise bulk.BulkError(f"Impossibile elencare i documenti (HTTP {status}).")
    return bulk._data(body) or []


async def prepare_plan(request, throttle, arguments: dict, data: bytes, filename: str,
                       previous: bulk.Table | None = None) -> bulk.Plan:
    """Shared front half of a bulk send: parse, fetch template, map, validate."""
    table = bulk.parse_table(data, filename)
    template = await bulk.fetch_template(request, str(arguments["source_document_id"]))
    mapping = bulk.build_mapping(table.headers, template, arguments.get("column_mapping"))
    if mapping.problems:
        raise MappingProblem(template, mapping, table)
    return bulk.plan_rows(table, mapping, template, previous)


class MappingProblem(bulk.BulkError):
    def __init__(self, template: bulk.Template, mapping: bulk.Mapping, table: bulk.Table):
        super().__init__("; ".join(mapping.problems))
        self.template, self.mapping, self.table = template, mapping, table

    def summary(self) -> dict:
        return {
            "errore": "Mappatura colonne incompleta: nessuna riga è stata inviata.",
            "problemi": self.mapping.problems,
            "colonne_del_file": self.table.headers,
            "firmatari_del_template": [s.label for s in self.template.signers],
            "campi_testo_del_template": [
                {"etichetta": f.label or f.id, "obbligatorio": f.required} for f in self.template.fields
            ],
            "suggerimento": "Correggi con column_mapping oppure rinomina le colonne del file.",
        }


# ===================================================================== local backend

def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class LocalBulkBackend:
    """Runs bulk operations inline, reading and writing files on the user's machine."""

    def __init__(self, request: Callable[..., Awaitable[Any]], time_budget: float = LOCAL_TIME_BUDGET,
                 min_interval: float = MIN_INTERVAL, sleep=None):
        self.request = request
        self.time_budget = time_budget
        self.min_interval = min_interval
        self.sleep = sleep

    def _throttle(self) -> bulk.Throttle:
        return bulk.Throttle(self.min_interval, **({"sleep": self.sleep} if self.sleep else {}))

    async def handle(self, name: str, arguments: dict) -> dict:
        if name == CREATE:
            if arguments.get("upload_id") and not arguments.get("file_path"):
                raise bulk.BulkError("upload_id vale solo per il connettore remoto: in locale usa file_path.")
            return await self.create(arguments)
        if name == DOWNLOAD:
            return await self.download(arguments)
        raise bulk.BulkError("Strumento disponibile solo nel connettore remoto.")

    # ------------------------------------------------------------------ send

    async def create(self, arguments: dict) -> dict:
        src = Path(str(arguments.get("file_path") or "")).expanduser()
        if not arguments.get("file_path") or not src.is_file():
            raise bulk.BulkError(f"File non trovato: {src}")
        if src.stat().st_size > MAX_INPUT_BYTES:
            raise bulk.BulkError("File troppo grande (massimo 20 MB).")
        outcome = src.with_name(bulk.outcome_filename(src.name))
        previous = None
        previous_mtime = 0.0
        if outcome.exists() and outcome != src:
            previous = bulk.parse_table(outcome.read_bytes(), outcome.name)
            previous_mtime = outcome.stat().st_mtime

        throttle = self._throttle()
        try:
            plan = await prepare_plan(self.request, throttle, arguments, src.read_bytes(), src.name, previous)
        except MappingProblem as e:
            return e.summary()

        dry_run = arguments.get("dry_run", True) is not False
        if dry_run:
            _atomic_write(outcome, plan.outcome_bytes())
            return bulk.summarize(plan, dry_run=True, outcome=str(outcome))

        # Rows interrupted by a previous crash: adopt the created document when unambiguous.
        await bulk.reconcile(plan, self.request, throttle, since=previous_mtime - 3600)

        async def persist(_row):
            _atomic_write(outcome, plan.outcome_bytes())

        deadline = time.monotonic() + self.time_budget
        message = None
        try:
            result = await bulk.send_rows(
                plan, self.request, throttle, arguments["email_subject"], arguments["email_body"],
                arguments.get("signature_type"), persist, deadline=deadline,
            )
        except bulk.StopJob as e:
            result, message = "interrotto", str(e)
        _atomic_write(outcome, plan.outcome_bytes())
        if result == "parziale":
            message = "Tempo della singola chiamata esaurito: richiama lo strumento con gli stessi parametri per continuare."
        return bulk.summarize(plan, dry_run=False, outcome=str(outcome), status=result, message=message)

    # -------------------------------------------------------------- download

    async def download(self, arguments: dict) -> dict:
        flt = download_filter(arguments)
        throttle = self._throttle()
        documents = await list_all_documents(self.request, throttle)
        selected, missing = bulk.select_documents(documents, flt)
        summary: dict[str, Any] = {"documenti_selezionati": len(selected)}
        if missing:
            summary["id_non_trovati"] = missing[: bulk.MAX_ERRORS_LISTED]
        if arguments.get("dry_run") or not selected:
            return summary

        if not arguments.get("destination_folder"):
            raise bulk.BulkError("Indica destination_folder, la cartella dove salvare i file.")
        dest = Path(str(arguments["destination_folder"])).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        with_copy = bool(arguments.get("include_reading_copy"))

        deadline = time.monotonic() + self.time_budget
        done = skipped = 0
        errors: list[str] = []
        remaining = 0
        for i, doc in enumerate(selected):
            base = bulk.readable_name(doc)
            zip_path = dest / f"{base}.zip"
            copy_path = dest / f"{base} (copia con audit trail).pdf"
            need_zip = not (zip_path.exists() and zip_path.stat().st_size > 0)
            need_copy = with_copy and not copy_path.exists()
            if not need_zip and not need_copy:
                skipped += 1
                continue
            if time.monotonic() >= deadline:
                remaining = len(selected) - i
                break
            if not need_zip:
                copy = bulk.merged_copy(zip_path.read_bytes())
                if copy:
                    _atomic_write(copy_path, copy)
                skipped += 1
                continue
            got = await bulk.fetch_bundle(self.request, throttle, doc, need_copy)
            if got.bundle:
                _atomic_write(zip_path, got.bundle)
                done += 1
            if got.copy:
                _atomic_write(copy_path, got.copy)
            if got.error:
                errors.append(f"{base}: {got.error}")

        summary.update({"scaricati": done, "gia_presenti": skipped, "cartella": str(dest)})
        if errors:
            summary["errori"] = len(errors)
            summary["errori_principali"] = errors[: bulk.MAX_ERRORS_LISTED]
        if remaining:
            summary["stato"] = "parziale"
            summary["ancora_da_scaricare"] = remaining
            summary["nota"] = "Tempo della singola chiamata esaurito: richiama lo strumento con gli stessi parametri per continuare."
        else:
            summary["stato"] = "completato"
        return summary
