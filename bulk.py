"""Bulk engine for the HEU MCP server.

Transport-agnostic logic shared by the local (stdio) and remote (multi-tenant)
servers:

* reading CSV/XLSX tables and writing the outcome ("esito") file;
* mapping table columns to a PDF template's signers and text fields;
* validating rows before anything is sent;
* sending rows one by one with throttling, retries and crash-safe resume;
* selecting documents and packaging their signed bundles for bulk download.

Nothing here knows about MCP, HTTP routing or where files live: callers pass
bytes in, get bytes out, and supply an async ``request`` callable with the same
contract as ``server._request`` (returns ``(status, body, headers)``).
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import re
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

RequestFn = Callable[..., Awaitable[tuple[int, Any, dict]]]

# --------------------------------------------------------------------------- limits

MAX_SIGNERS = 10
MAX_FULL_NAME = 100
MAX_DOCUMENT_NAME = 120
MAX_ROWS = 5000
MAX_ERRORS_LISTED = 20

# Outcome columns appended to the user's table. Values are Italian because the
# outcome file is read by the end user.
COL_DOCUMENT_ID = "document_id"
COL_STATE = "stato"
COL_ERROR = "errore"
OUTCOME_COLUMNS = (COL_DOCUMENT_ID, COL_STATE, COL_ERROR)

STATE_PENDING = "in_attesa"
STATE_IN_PROGRESS = "in_corso"
STATE_SENT = "inviato"
STATE_FAILED = "errore"
STATE_INVALID = "non_valido"
STATE_TO_VERIFY = "da_verificare"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


class BulkError(Exception):
    """A problem that stops the whole operation (bad file, unusable template...)."""


# ============================================================ table input / output

@dataclass
class Table:
    headers: list[str]
    rows: list[dict[str, str]]
    fmt: str  # "csv" or "xlsx"
    delimiter: str = ","

    @property
    def row_numbers(self) -> list[int]:
        # Spreadsheet-style numbering: header is row 1, first data row is row 2.
        return list(range(2, len(self.rows) + 2))


def table_format(filename: str) -> str:
    name = filename.lower()
    if name.endswith(".csv") or name.endswith(".txt"):
        return "csv"
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        return "xlsx"
    raise BulkError("Formato non supportato: usa un file .csv o .xlsx.")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value).strip()


def _decode_csv(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise BulkError("Impossibile leggere il CSV: codifica non riconosciuta.")


def _unique_headers(raw: list[str]) -> list[str]:
    headers, seen = [], {}
    for i, h in enumerate(raw):
        h = _cell(h) or f"colonna_{i + 1}"
        if h in seen:
            seen[h] += 1
            h = f"{h} ({seen[h]})"
        else:
            seen[h] = 1
        headers.append(h)
    return headers


def parse_table(data: bytes, filename: str) -> Table:
    """Parse a CSV or XLSX file into headers + rows. Fully empty rows are dropped."""
    fmt = table_format(filename)
    if not data:
        raise BulkError("Il file è vuoto.")

    if fmt == "csv":
        text = _decode_csv(data)
        sample = text[:4096]
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
        except csv.Error:
            delimiter = ";" if sample.count(";") > sample.count(",") else ","
        matrix = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    else:
        delimiter = ","
        try:
            from openpyxl import load_workbook
        except ImportError as e:  # pragma: no cover - dependency is declared
            raise BulkError("Supporto .xlsx non disponibile (manca openpyxl).") from e
        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as e:
            raise BulkError(f"Impossibile leggere il file Excel: {e}") from e
        ws = wb.worksheets[0]
        matrix = [[_cell(c) for c in row] for row in ws.iter_rows(values_only=True)]
        wb.close()

    matrix = [[_cell(c) for c in row] for row in matrix]
    while matrix and not any(matrix[0]):
        matrix.pop(0)
    if not matrix:
        raise BulkError("Il file non contiene righe.")

    headers = _unique_headers(matrix[0])
    rows: list[dict[str, str]] = []
    for raw in matrix[1:]:
        if not any(raw):
            continue
        raw = raw + [""] * (len(headers) - len(raw))
        rows.append({h: raw[i] for i, h in enumerate(headers)})
    if not rows:
        raise BulkError("Il file contiene solo l'intestazione, nessuna riga di dati.")
    if len(rows) > MAX_ROWS:
        raise BulkError(f"Troppe righe ({len(rows)}): il massimo per un invio è {MAX_ROWS}.")
    return Table(headers=headers, rows=rows, fmt=fmt, delimiter=delimiter)


def write_table(headers: list[str], rows: list[dict[str, str]], fmt: str, delimiter: str = ",") -> bytes:
    """Serialize rows back to CSV (same delimiter, UTF-8 with BOM for Excel) or XLSX."""
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
        writer.writerow(headers)
        for row in rows:
            writer.writerow([row.get(h, "") for h in headers])
        return ("﻿" + buf.getvalue()).encode("utf-8")

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Esito"
    ws.append(headers)
    for row in rows:
        ws.append([row.get(h, "") for h in headers])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def outcome_filename(input_name: str) -> str:
    """'contratti.xlsx' -> 'contratti.esito.xlsx'. Re-running on an outcome file keeps its name."""
    base, dot, ext = input_name.rpartition(".")
    if not dot:
        return f"{input_name}.esito"
    if base.endswith(".esito"):
        return input_name
    return f"{base}.esito.{ext}"


# ======================================================================== templates

def norm(text: str) -> str:
    """Lowercase, strip accents and collapse punctuation: 'E-mail Cliente' -> 'e mail cliente'."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


@dataclass
class TemplateSigner:
    id: str
    label: str


@dataclass
class TemplateField:
    id: str
    signer_id: str
    type: str
    label: str
    required: bool


@dataclass
class Template:
    id: str
    name: str
    signers: list[TemplateSigner]
    fields: list[TemplateField]  # text placeholders only: the ones a table can fill

    def signer_label(self, signer_id: str) -> str:
        for s in self.signers:
            if s.id == signer_id:
                return s.label
        return signer_id


def _data(body: Any) -> Any:
    return body.get("data", body) if isinstance(body, dict) else body


async def fetch_template(request: RequestFn, source_id: str) -> Template:
    status, body, _ = await request("GET", f"/pdfs/{source_id}")
    if status != 200:
        raise BulkError(f"Template {source_id} non trovato o non accessibile (HTTP {status}).")
    meta = _data(body) or {}
    if meta.get("type") not in (None, "template"):
        raise BulkError(f"Il documento {source_id} non è un template (tipo: {meta.get('type')}).")

    status, body, _ = await request("GET", f"/pdfs/{source_id}/signers")
    if status != 200:
        raise BulkError(f"Impossibile leggere i firmatari del template (HTTP {status}).")
    signers = [
        TemplateSigner(id=str(s.get("id")), label=(s.get("full_name") or f"Firmatario {i + 1}").strip())
        for i, s in enumerate(_data(body) or [])
    ]
    if not signers:
        raise BulkError("Il template non ha firmatari.")

    status, body, _ = await request("GET", f"/pdfs/{source_id}/placeholders")
    if status != 200:
        raise BulkError(f"Impossibile leggere i campi del template (HTTP {status}).")
    fields = [
        TemplateField(
            id=str(p.get("id")),
            signer_id=str(p.get("signer_id")),
            type=p.get("type") or "",
            label=(p.get("text_label") or "").strip(),
            required=p.get("required") is not False,
        )
        for p in (_data(body) or [])
        if p.get("type") == "text"
    ]
    return Template(id=str(source_id), name=meta.get("name") or "Documento", signers=signers, fields=fields)


# ========================================================================== mapping

NAME_WORDS = ("nome completo", "nominativo", "full name", "name", "nome", "ragione sociale")
FIRST_NAME_WORDS = ("nome", "first name")
LAST_NAME_WORDS = ("cognome", "last name", "surname")
EMAIL_WORDS = ("email", "e mail", "mail", "indirizzo email", "pec")


@dataclass
class SignerMapping:
    signer_id: str
    label: str
    name_columns: list[str] = field(default_factory=list)  # one full-name column, or first+last
    email_column: str | None = None


@dataclass
class Mapping:
    signers: list[SignerMapping]
    fields: dict[str, str]  # placeholder id -> column
    document_name: str | None  # column name or pattern with {Column} placeholders
    problems: list[str] = field(default_factory=list)

    def describe(self, template: Template) -> dict:
        """Compact, human-readable view of the mapping for the dry-run summary."""
        labels = {f.id: f.label or f.id for f in template.fields}
        return {
            "firmatari": {
                s.label: {"nome": " + ".join(s.name_columns) or None, "email": s.email_column}
                for s in self.signers
            },
            "campi": {labels.get(fid, fid): col for fid, col in self.fields.items()},
            "nome_documento": self.document_name,
        }


def _find(headers: list[str], candidates: list[str]) -> str | None:
    by_norm = {norm(h): h for h in headers}
    for c in candidates:
        if norm(c) in by_norm:
            return by_norm[norm(c)]
    return None


def _resolve_column(headers: list[str], name: str | None) -> str | None:
    if not name:
        return None
    return _find(headers, [name])


def build_mapping(headers: list[str], template: Template, overrides: dict | None = None) -> Mapping:
    """Match columns to template signers and text fields.

    Defaults are derived from header names (case/accent-insensitive). ``overrides``
    takes precedence and has the shape::

        {"firmatari": {"<signer label or id>": {"nome": "Col", "email": "Col"}},
         "campi": {"<field label or id>": "Col"},
         "nome_documento": "Col" | "Contratto {Ragione sociale}"}

    English keys ("signers", "name", "fields", "document_name") are accepted too.
    """
    overrides = overrides or {}
    o_signers = overrides.get("firmatari") or overrides.get("signers") or {}
    o_fields = overrides.get("campi") or overrides.get("fields") or {}
    o_docname = overrides.get("nome_documento") or overrides.get("document_name")
    problems: list[str] = []
    single = len(template.signers) == 1

    signer_maps: list[SignerMapping] = []
    for s in template.signers:
        sm = SignerMapping(signer_id=s.id, label=s.label)
        override = None
        for key, val in o_signers.items():
            if key == s.id or norm(key) == norm(s.label):
                override = val or {}
        if override is not None:
            name_cols = override.get("nome") or override.get("name")
            if isinstance(name_cols, str):
                name_cols = [name_cols]
            for col in name_cols or []:
                resolved = _resolve_column(headers, col)
                if resolved:
                    sm.name_columns.append(resolved)
                else:
                    problems.append(f"Colonna '{col}' indicata per il nome di '{s.label}' non trovata.")
            email_col = override.get("email")
            if email_col:
                sm.email_column = _resolve_column(headers, email_col)
                if not sm.email_column:
                    problems.append(f"Colonna '{email_col}' indicata per l'email di '{s.label}' non trovata.")

        lab = s.label
        if not sm.name_columns:
            cand = [f"{w} {lab}" for w in NAME_WORDS] + [f"{lab} {w}" for w in NAME_WORDS] + [lab]
            if single:
                cand += list(NAME_WORDS)
            full = _find(headers, cand)
            if full and norm(full) not in {norm(w) for w in FIRST_NAME_WORDS}:
                sm.name_columns = [full]
            else:
                first = _find(headers, [f"{w} {lab}" for w in FIRST_NAME_WORDS] + [f"{lab} {w}" for w in FIRST_NAME_WORDS]
                              + (list(FIRST_NAME_WORDS) if single else []))
                last = _find(headers, [f"{w} {lab}" for w in LAST_NAME_WORDS] + [f"{lab} {w}" for w in LAST_NAME_WORDS]
                             + (list(LAST_NAME_WORDS) if single else []))
                if first and last:
                    sm.name_columns = [first, last]
                elif full or first:
                    sm.name_columns = [full or first]
        if not sm.email_column:
            cand = [f"{w} {lab}" for w in EMAIL_WORDS] + [f"{lab} {w}" for w in EMAIL_WORDS]
            if single:
                cand += list(EMAIL_WORDS)
            sm.email_column = _find(headers, cand)

        if not sm.name_columns:
            problems.append(f"Nessuna colonna per il nome del firmatario '{lab}'.")
        if not sm.email_column:
            problems.append(f"Nessuna colonna per l'email del firmatario '{lab}'.")
        signer_maps.append(sm)

    field_map: dict[str, str] = {}
    for f in template.fields:
        col = None
        for key, val in o_fields.items():
            if key == f.id or (f.label and norm(key) == norm(f.label)):
                col = _resolve_column(headers, val)
                if not col:
                    problems.append(f"Colonna '{val}' indicata per il campo '{f.label or f.id}' non trovata.")
        if not col and f.label:
            col = _find(headers, [f.label])
        if col:
            field_map[f.id] = col
        elif f.required:
            problems.append(
                f"Campo obbligatorio '{f.label or f.id}' (firmatario '{template.signer_label(f.signer_id)}') "
                "senza colonna corrispondente."
            )

    doc_name = None
    if o_docname:
        doc_name = _resolve_column(headers, o_docname) if "{" not in o_docname else o_docname
        if not doc_name:
            problems.append(f"Colonna '{o_docname}' indicata per il nome documento non trovata.")
    else:
        doc_name = _find(headers, ["nome documento", "document name", "titolo", "nome contratto"])

    return Mapping(signers=signer_maps, fields=field_map, document_name=doc_name, problems=problems)


def _fill_pattern(pattern: str, row: dict[str, str]) -> str:
    by_norm = {norm(k): v for k, v in row.items()}

    def repl(m: re.Match) -> str:
        return by_norm.get(norm(m.group(1)), "")

    return re.sub(r"\{([^{}]+)\}", repl, pattern).strip()


def row_document_name(row: dict[str, str], mapping: Mapping, template: Template) -> str:
    if mapping.document_name:
        if "{" in mapping.document_name:
            return _fill_pattern(mapping.document_name, row)
        return row.get(mapping.document_name, "").strip()
    # Default: template name + first signer, so each document is recognisable.
    first = mapping.signers[0]
    who = " ".join(row.get(c, "").strip() for c in first.name_columns).strip()
    return f"{template.name} - {who}" if who else template.name


def row_signers(row: dict[str, str], mapping: Mapping) -> list[dict[str, str]]:
    out = []
    for sm in mapping.signers:
        name = " ".join(row.get(c, "").strip() for c in sm.name_columns).strip()
        email = row.get(sm.email_column or "", "").strip()
        out.append({"source_id": sm.signer_id, "full_name": name, "email": email})
    return out


def row_fingerprint(row: dict[str, str], template_id: str) -> str:
    """Identity of a row for safe resume: template + the row's own cells.

    Deliberately independent of the column mapping, so changing column_mapping or
    the document-name pattern between runs never makes a sent row look new.
    Outcome columns are excluded, so an outcome file and its original match.
    """
    cells = sorted((norm(k), " ".join(v.split())) for k, v in row.items() if k not in OUTCOME_COLUMNS)
    payload = json.dumps({"t": template_id, "c": cells}, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def duplicate_key(row: dict[str, str], mapping: Mapping, template: Template) -> str:
    """What a row would actually send: two rows with the same key are duplicates."""
    payload = {
        "s": [(s["full_name"].lower(), _email_key(s["email"])) for s in row_signers(row, mapping)],
        "f": sorted((fid, row.get(col, "").strip()) for fid, col in mapping.fields.items()),
        "n": row_document_name(row, mapping, template),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def _email_key(email: str) -> str:
    return email.strip().strip("\"'").lower()


def validate_row(row: dict[str, str], mapping: Mapping, template: Template) -> list[str]:
    errors: list[str] = []
    seen_emails: dict[str, str] = {}
    for s, sm in zip(row_signers(row, mapping), mapping.signers):
        if not s["full_name"]:
            errors.append(f"nome di '{sm.label}' mancante")
        elif len(s["full_name"]) > MAX_FULL_NAME:
            errors.append(f"nome di '{sm.label}' oltre {MAX_FULL_NAME} caratteri")
        if not s["email"]:
            errors.append(f"email di '{sm.label}' mancante")
        elif not EMAIL_RE.match(s["email"]):
            errors.append(f"email di '{sm.label}' non valida ({s['email']})")
        else:
            key = _email_key(s["email"])
            if key in seen_emails:
                errors.append(f"'{sm.label}' e '{seen_emails[key]}' hanno la stessa email")
            seen_emails[key] = sm.label
    for f in template.fields:
        col = mapping.fields.get(f.id)
        if f.required and col and not row.get(col, "").strip():
            errors.append(f"campo obbligatorio '{f.label or f.id}' vuoto")
    name = row_document_name(row, mapping, template)
    if len(name) > MAX_DOCUMENT_NAME:
        errors.append(f"nome documento oltre {MAX_DOCUMENT_NAME} caratteri")
    return errors


def build_payload(
    row: dict[str, str],
    mapping: Mapping,
    template: Template,
    email_subject: str,
    email_body: str,
    signature_type: str | None = None,
) -> dict:
    payload: dict[str, Any] = {
        "source_document_id": template.id,
        "document_name": row_document_name(row, mapping, template),
        "email": {"subject": email_subject, "body": email_body},
        "signers": row_signers(row, mapping),
    }
    if signature_type:
        payload["signature_type"] = signature_type
    placeholders = [
        {"source_id": fid, "text_value": row.get(col, "").strip()}
        for fid, col in mapping.fields.items()
        if row.get(col, "").strip()
    ]
    if placeholders:
        payload["placeholders"] = placeholders
    return payload


# ============================================================================ plan

@dataclass
class RowState:
    number: int  # spreadsheet row number (header = 1)
    data: dict[str, str]
    fingerprint: str
    state: str = STATE_PENDING
    document_id: str = ""
    error: str = ""


@dataclass
class Plan:
    template: Template
    mapping: Mapping
    rows: list[RowState]
    table: Table

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            out[r.state] = out.get(r.state, 0) + 1
        return out

    def errors(self, limit: int = MAX_ERRORS_LISTED) -> list[str]:
        bad = [r for r in self.rows if r.state in (STATE_INVALID, STATE_FAILED, STATE_TO_VERIFY)]
        return [f"riga {r.number}: {r.error}" for r in bad[:limit]]

    def outcome_rows(self) -> list[dict[str, str]]:
        out = []
        for r in self.rows:
            row = {k: v for k, v in r.data.items() if k not in OUTCOME_COLUMNS}
            row.update({COL_DOCUMENT_ID: r.document_id, COL_STATE: r.state, COL_ERROR: r.error})
            out.append(row)
        return out

    def outcome_headers(self) -> list[str]:
        return [h for h in self.table.headers if h not in OUTCOME_COLUMNS] + list(OUTCOME_COLUMNS)

    def outcome_bytes(self) -> bytes:
        return write_table(self.outcome_headers(), self.outcome_rows(), self.table.fmt, self.table.delimiter)


def plan_rows(table: Table, mapping: Mapping, template: Template, previous: Table | None = None) -> Plan:
    """Validate every row and carry over outcomes from earlier runs.

    Two sources of history, both conservative (a sent row is never sent again):

    * the input itself, when the user re-submits an outcome file: a row whose own
      ``stato`` cell says it was sent (or may have been) is not sent again;
    * ``previous``, the outcome file written next to the input by an earlier run:
      rows are matched by content; a row that was sent and has since been edited
      is held as "da verificare" instead of being sent a second time.
    """
    carried_states = (STATE_SENT, STATE_IN_PROGRESS, STATE_TO_VERIFY)
    prev_by_fp: dict[str, tuple[str, str, str]] = {}
    prev_by_number: dict[int, tuple[str, str, str]] = {}
    if previous is not None and COL_STATE in previous.headers:
        for number, r in zip(previous.row_numbers, previous.rows):
            state = (r.get(COL_STATE) or "").strip()
            if state in carried_states:
                entry = (state, (r.get(COL_DOCUMENT_ID) or "").strip(), (r.get(COL_ERROR) or "").strip())
                prev_by_fp.setdefault(row_fingerprint(r, template.id), entry)
                prev_by_number[number] = entry
    own_outcome = COL_STATE in table.headers

    rows: list[RowState] = []
    first_seen: dict[str, int] = {}
    for number, data in zip(table.row_numbers, table.rows):
        fp = row_fingerprint(data, template.id)
        key = duplicate_key(data, mapping, template)
        rs = RowState(number=number, data=data, fingerprint=fp)
        entry = None
        if own_outcome and (data.get(COL_STATE) or "").strip() in carried_states:
            entry = ((data.get(COL_STATE) or "").strip(), (data.get(COL_DOCUMENT_ID) or "").strip(),
                     (data.get(COL_ERROR) or "").strip())
        elif fp in prev_by_fp:
            entry = prev_by_fp[fp]
        elif number in prev_by_number and prev_by_number[number][0] == STATE_SENT:
            doc = prev_by_number[number][1]
            entry = (STATE_TO_VERIFY, doc,
                     f"riga già inviata (documento {doc or 'sconosciuto'}) e poi modificata: verificare prima di "
                     "reinviare")
        if entry:
            state, doc_id, err = entry
            rs.document_id = doc_id
            if state == STATE_SENT:
                rs.state = STATE_SENT
            elif state == STATE_IN_PROGRESS:
                # Interrupted mid-request last time: the document may exist. Never resend blindly.
                rs.state, rs.error = STATE_TO_VERIFY, "invio interrotto: verificare se il documento è stato creato"
            else:
                rs.state, rs.error = STATE_TO_VERIFY, err or "da verificare"
        else:
            errors = validate_row(data, mapping, template)
            if key in first_seen:
                errors.append(f"duplicato della riga {first_seen[key]}")
            if errors:
                rs.state, rs.error = STATE_INVALID, "; ".join(errors)
        first_seen.setdefault(key, number)
        rows.append(rs)
    return Plan(template=template, mapping=mapping, rows=rows, table=table)


# ========================================================================= sending

class Throttle:
    """Minimum interval between upstream calls (HEU allows 300 requests / 5 minutes)."""

    def __init__(self, min_interval: float, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.min_interval = min_interval
        self._sleep = sleep
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await self._sleep(self._next - now)
            self._next = max(now, self._next) + self.min_interval

    def penalize(self, seconds: float) -> None:
        self._next = max(self._next, time.monotonic() + seconds)


class StopJob(Exception):
    """An error that will hit every remaining row too (revoked key, no FEA credits)."""


def _retry_after(headers: dict, default: float) -> float:
    for k, v in (headers or {}).items():
        if k.lower() == "retry-after":
            try:
                return min(max(float(v), 1.0), 300.0)
            except ValueError:
                break
    return default


def _error_text(status: int, body: Any) -> str:
    if isinstance(body, dict):
        for key in ("message", "error", "detail"):
            val = body.get(key)
            if isinstance(val, dict):
                val = val.get("message") or json.dumps(val, ensure_ascii=False)
            if val:
                return f"HTTP {status}: {str(val)[:200]}"
        return f"HTTP {status}: {json.dumps(body, ensure_ascii=False)[:200]}"
    if isinstance(body, (bytes, bytearray)):
        return f"HTTP {status}"
    return f"HTTP {status}: {str(body)[:200]}"


async def call_api(
    request: RequestFn,
    method: str,
    path: str,
    throttle: Throttle,
    *,
    attempts: int = 4,
    backoff: tuple[float, ...] = (2.0, 5.0, 15.0),
    sleep: Callable[[float], Awaitable[None]] | None = None,
    **kwargs,
) -> tuple[int, Any, dict]:
    """Call the API honouring the throttle, Retry-After on 429 and retrying 5xx/network errors.

    4xx answers (other than 429) are returned as-is: they are about this request
    and retrying would not change them.
    """
    sleep = sleep or throttle._sleep
    rate_limited = 0
    failures = 0
    while True:
        await throttle.wait()
        try:
            status, body, headers = await request(method, path, **kwargs)
        except Exception as e:  # network error, timeout
            failures += 1
            if failures >= attempts:
                return 0, f"errore di rete: {e}", {}
            await sleep(backoff[min(failures - 1, len(backoff) - 1)])
            continue
        if status == 429:
            rate_limited += 1
            wait = _retry_after(headers, 60.0)
            throttle.penalize(wait)
            if rate_limited > 8:
                return status, body, headers
            await sleep(0)
            continue
        if status >= 500:
            failures += 1
            if failures >= attempts:
                return status, body, headers
            await sleep(backoff[min(failures - 1, len(backoff) - 1)])
            continue
        return status, body, headers


async def find_created_document(request: RequestFn, throttle: Throttle, name: str, since: float) -> str | None:
    """After an interrupted send, look for a document with this exact name created since ``since``."""
    status, body, _ = await call_api(request, "GET", "/pdfs", throttle, params={"type": "document"})
    if status != 200:
        return None
    matches = []
    for d in _data(body) or []:
        if (d.get("name") or "").strip() != name.strip():
            continue
        created = _parse_ts(d.get("created_at"))
        if created is None or created >= since - 300:
            matches.append(str(d.get("id")))
    return matches[0] if len(matches) == 1 else None


def _parse_ts(value: Any) -> float | None:
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


async def reconcile(plan: Plan, request: RequestFn, throttle: Throttle, since: float) -> None:
    """Resolve rows left 'in progress' by a crash: adopt the created document if unambiguous."""
    for r in plan.rows:
        if r.state == STATE_TO_VERIFY and r.error.startswith("invio interrotto"):
            name = row_document_name(r.data, plan.mapping, plan.template)
            doc_id = await find_created_document(request, throttle, name, since)
            if doc_id:
                r.state, r.document_id, r.error = STATE_SENT, doc_id, ""


async def send_rows(
    plan: Plan,
    request: RequestFn,
    throttle: Throttle,
    email_subject: str,
    email_body: str,
    signature_type: str | None,
    on_change: Callable[[RowState], Awaitable[None]],
    deadline: float | None = None,
) -> str:
    """Send every pending row. Returns 'completato', 'parziale' (deadline hit) or 'interrotto'.

    ``on_change`` is awaited before each request (row marked in progress) and after
    it (final state), so the caller can persist progress and a crash never loses
    track of a row that may already have been created.
    """
    for r in plan.rows:
        if r.state != STATE_PENDING:
            continue
        if deadline is not None and time.monotonic() >= deadline:
            return "parziale"
        payload = build_payload(r.data, plan.mapping, plan.template, email_subject, email_body, signature_type)
        r.state = STATE_IN_PROGRESS
        await on_change(r)
        status, body, _ = await call_api(request, "POST", "/pdfs", throttle, json=payload)
        if status in (200, 201):
            data = _data(body) or {}
            r.state, r.document_id, r.error = STATE_SENT, str(data.get("id", "")), ""
            await on_change(r)
            continue
        if status == 0 or status >= 500:
            # Unknown outcome: the server may have created the document before failing.
            r.state, r.error = STATE_TO_VERIFY, _error_text(status, body) + " (esito incerto, non reinviato)"
        else:
            r.state, r.error = STATE_FAILED, _error_text(status, body)
        await on_change(r)
        if status in (401, 403):
            raise StopJob("API key non valida o revocata: invio interrotto.")
        if status == 422 and "fea" in json.dumps(body, ensure_ascii=False).lower():
            raise StopJob("Crediti FEA insufficienti: invio interrotto, le righe rimanenti sono ancora da inviare.")
    return "completato"


def summarize(plan: Plan, *, dry_run: bool, outcome: str | None = None, status: str | None = None,
              message: str | None = None) -> dict:
    counts = plan.counts()
    summary: dict[str, Any] = {
        "modalita": "prova (nessun invio)" if dry_run else "invio",
        "template": plan.template.name,
        "righe_totali": len(plan.rows),
    }
    if dry_run:
        summary["righe_valide"] = counts.get(STATE_PENDING, 0)
        summary["righe_con_errori"] = counts.get(STATE_INVALID, 0)
        summary["gia_inviate"] = counts.get(STATE_SENT, 0)
        summary["da_verificare"] = counts.get(STATE_TO_VERIFY, 0)
        summary["mappatura"] = plan.mapping.describe(plan.template)
    else:
        summary["inviate"] = counts.get(STATE_SENT, 0)
        summary["errori"] = counts.get(STATE_FAILED, 0) + counts.get(STATE_INVALID, 0)
        summary["da_verificare"] = counts.get(STATE_TO_VERIFY, 0)
        summary["ancora_da_inviare"] = counts.get(STATE_PENDING, 0)
    if status:
        summary["stato"] = status
    errors = plan.errors()
    if errors:
        summary["errori_principali"] = errors
        hidden = sum(1 for r in plan.rows if r.state in (STATE_INVALID, STATE_FAILED, STATE_TO_VERIFY)) - len(errors)
        if hidden > 0:
            summary["altri_errori"] = f"{hidden} non elencati, vedi file di esito"
    if outcome:
        summary["file_esito"] = outcome
    if message:
        summary["nota"] = message
    return summary


# ================================================================= bulk download

@dataclass
class DownloadFilter:
    document_ids: list[str] = field(default_factory=list)
    folder: str | None = None  # HEU tag
    status: str | None = "signed"
    created_from: str | None = None
    created_to: str | None = None
    name_contains: str | None = None


def _day(value: str | None, end: bool) -> float | None:
    if not value:
        return None
    from datetime import datetime, timezone

    text = value.strip()
    try:
        if len(text) == 10:
            dt = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
            if end:
                dt = dt.replace(hour=23, minute=59, second=59)
        else:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise BulkError(f"Data non valida: {value} (usa AAAA-MM-GG).") from e
    return dt.timestamp()


def select_documents(documents: list[dict], flt: DownloadFilter) -> tuple[list[dict], list[str]]:
    """Apply the filter client-side (the API cannot filter). Returns (selected, missing ids)."""
    if flt.document_ids:
        wanted = [str(i).strip() for i in flt.document_ids if str(i).strip()]
        by_id = {str(d.get("id")): d for d in documents}
        return [by_id[i] for i in wanted if i in by_id], [i for i in wanted if i not in by_id]

    start, end = _day(flt.created_from, False), _day(flt.created_to, True)
    folder = norm(flt.folder) if flt.folder else None
    needle = norm(flt.name_contains) if flt.name_contains else None
    out = []
    for d in documents:
        if flt.status and flt.status != "any" and d.get("status") != flt.status:
            continue
        if folder is not None:
            tags = d.get("tags") or []
            names = [norm(t if isinstance(t, str) else (t.get("name") or "")) for t in tags]
            if folder not in names:
                continue
        if needle and needle not in norm(d.get("name") or ""):
            continue
        created = _parse_ts(d.get("created_at"))
        if start is not None and (created is None or created < start):
            continue
        if end is not None and (created is None or created > end):
            continue
        out.append(d)
    return out, []


def readable_name(document: dict) -> str:
    """'Contratto Rossi / 2026' + id 88 -> 'Contratto Rossi 2026 - 88'."""
    name = unicodedata.normalize("NFKC", document.get("name") or "documento")
    name = re.sub(r"\.pdf$", "", name, flags=re.I)
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")[:80] or "documento"
    return f"{name} - {document.get('id')}"


def merged_copy(bundle: bytes) -> bytes | None:
    """Signed PDF followed by its audit trail, from a bundle ZIP.

    Rewriting the PDF invalidates any embedded digital signature, so this is a
    reading copy only; the original bundle stays the legally relevant artefact.
    Returns None when the bundle does not contain both parts.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(bundle))
    except zipfile.BadZipFile:
        return None
    pdfs = [i for i in zf.infolist() if i.filename.lower().endswith(".pdf") and not i.is_dir()]
    audit = [i for i in pdfs if "audit" in i.filename.lower()]
    others = [i for i in pdfs if i not in audit]
    if not audit or not others:
        return None
    main = max(others, key=lambda i: i.file_size)
    return merge_pdfs([zf.read(main), zf.read(audit[0])])


@dataclass
class FetchedBundle:
    document: dict
    bundle: bytes | None = None
    copy: bytes | None = None
    error: str = ""


async def fetch_bundle(
    request: RequestFn, throttle: Throttle, document: dict, with_copy: bool
) -> FetchedBundle:
    """Download one document's bundle and, optionally, build the single-PDF reading copy.

    If the bundle does not contain both the signed PDF and the audit trail, the
    copy is built from the dedicated /download and /audit-trail endpoints instead.
    """

    doc_id = str(document.get("id"))
    result = FetchedBundle(document=document)
    status, body, _h = await call_api(request, "GET", f"/pdfs/{doc_id}/bundle", throttle, timeout=180.0)
    if status != 200 or not isinstance(body, (bytes, bytearray)):
        result.error = _error_text(status, body) if status else str(body)
        if status == 401:
            result.error = "non sei destinatario di questo documento (HTTP 401)"
        return result
    result.bundle = bytes(body)
    if not with_copy:
        return result

    copy = merged_copy(result.bundle)
    if copy is None:
        parts = []
        for suffix in ("download", "audit-trail"):
            s, b, _h = await call_api(request, "GET", f"/pdfs/{doc_id}/{suffix}", throttle, timeout=180.0)
            if s != 200 or not isinstance(b, (bytes, bytearray)):
                parts = []
                break
            parts.append(bytes(b))
        if len(parts) == 2:
            copy = merge_pdfs(parts)
    if copy is None:
        result.error = "copia unica non generata (audit trail non disponibile); bundle scaricato"
    result.copy = copy
    return result


def merge_pdfs(parts: list[bytes]) -> bytes | None:
    from pypdf import PdfReader, PdfWriter

    try:
        writer = PdfWriter()
        for p in parts:
            writer.append(PdfReader(io.BytesIO(p)))
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception:
        return None
