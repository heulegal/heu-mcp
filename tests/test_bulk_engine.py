"""Bulk engine: parsing, mapping, validation, sending, resume, download selection."""
import io

import pytest
from openpyxl import Workbook, load_workbook

import bulk
from conftest import make_pdf
from fakes import FakeHeuApi, make_bundle, no_sleep

pytestmark = pytest.mark.anyio


def csv_bytes(text: str, encoding="utf-8") -> bytes:
    return text.encode(encoding)


def xlsx_bytes(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ parsing

def test_parse_csv_semicolon_bom_and_blank_rows():
    data = "﻿Nome;Email;Indirizzo\nMario Rossi;mario@x.it;Via Roma 1\n;;\nAnna Bianchi;anna@x.it;Via Po 2\n"
    t = bulk.parse_table(csv_bytes(data), "lista.csv")
    assert t.headers == ["Nome", "Email", "Indirizzo"]
    assert t.delimiter == ";"
    assert [r["Nome"] for r in t.rows] == ["Mario Rossi", "Anna Bianchi"]
    assert t.row_numbers == [2, 3]


def test_parse_csv_comma_and_cp1252():
    data = "Nome,Email,Indirizzo\nNiccolò Rossi,n@x.it,Città\n"
    t = bulk.parse_table(csv_bytes(data, "cp1252"), "lista.CSV")
    assert t.delimiter == ","
    assert t.rows[0]["Nome"] == "Niccolò Rossi"


def test_parse_xlsx_numbers_and_short_rows():
    data = xlsx_bytes([["Nome", "Email", "CAP"], ["Mario", "m@x.it", 20100], ["Anna", "a@x.it"]])
    t = bulk.parse_table(data, "lista.xlsx")
    assert t.fmt == "xlsx"
    assert t.rows[0]["CAP"] == "20100"
    assert t.rows[1]["CAP"] == ""


def test_parse_rejects_bad_inputs():
    with pytest.raises(bulk.BulkError):
        bulk.parse_table(b"", "a.csv")
    with pytest.raises(bulk.BulkError):
        bulk.parse_table(b"Nome,Email\n", "a.csv")
    with pytest.raises(bulk.BulkError):
        bulk.parse_table(b"x", "a.pdf")


def test_outcome_roundtrip_xlsx_and_csv():
    headers = ["Nome", "stato"]
    rows = [{"Nome": "Mario", "stato": "inviato"}]
    back = bulk.parse_table(bulk.write_table(headers, rows, "xlsx"), "x.xlsx")
    assert back.rows == rows
    back = bulk.parse_table(bulk.write_table(headers, rows, "csv", ";"), "x.csv")
    assert back.rows == rows and back.delimiter == ";"
    assert bulk.outcome_filename("lista.xlsx") == "lista.esito.xlsx"
    assert bulk.outcome_filename("lista.esito.xlsx") == "lista.esito.xlsx"


# ------------------------------------------------------------------ mapping

async def template(api=None):
    return await bulk.fetch_template(api or FakeHeuApi(), "tpl1")


async def test_mapping_single_signer_defaults():
    tpl = await template()
    assert [f.label for f in tpl.fields] == ["Indirizzo", "Note"]  # signature excluded
    m = bulk.build_mapping(["Nome", "E-mail", "INDIRIZZO"], tpl)
    assert m.signers[0].name_columns == ["Nome"]
    assert m.signers[0].email_column == "E-mail"
    assert m.fields == {"p2": "INDIRIZZO"}
    assert m.problems == []


async def test_mapping_first_and_last_name():
    tpl = await template()
    m = bulk.build_mapping(["Nome", "Cognome", "Email", "Indirizzo"], tpl)
    assert m.signers[0].name_columns == ["Nome", "Cognome"]


async def test_mapping_multi_signer_by_label():
    api = FakeHeuApi(
        signers=[{"id": "s1", "full_name": "Cliente"}, {"id": "s2", "full_name": "Fornitore"}],
        placeholders=[],
    )
    tpl = await template(api)
    m = bulk.build_mapping(["Nome Cliente", "Email Cliente", "Fornitore", "Fornitore email"], tpl)
    assert m.signers[0].name_columns == ["Nome Cliente"] and m.signers[0].email_column == "Email Cliente"
    assert m.signers[1].name_columns == ["Fornitore"] and m.signers[1].email_column == "Fornitore email"
    assert m.problems == []


async def test_mapping_reports_missing_signer_columns_and_overrides():
    tpl = await template()
    m = bulk.build_mapping(["Nominativo", "Posta"], tpl)
    assert any("email" in p for p in m.problems)
    # A text field without a column is not a problem: the signer fills it in while signing.
    assert not any("Indirizzo" in p for p in m.problems)
    assert m.describe(tpl)["campi_compilati_dal_firmatario"] == ["Indirizzo", "Note"]
    m = bulk.build_mapping(
        ["Nominativo", "Posta", "Via"], tpl,
        {"firmatari": {"Cliente": {"email": "Posta"}}, "campi": {"Indirizzo": "Via"},
         "nome_documento": "Contratto {Nominativo}"},
    )
    assert m.problems == []
    assert m.signers[0].email_column == "Posta" and m.fields["p2"] == "Via"
    assert bulk.row_document_name({"Nominativo": "Mario"}, m, tpl) == "Contratto Mario"


# --------------------------------------------------------------- validation

async def test_validation_and_duplicates():
    tpl = await template()
    t = bulk.parse_table(csv_bytes(
        "Nome;Email;Indirizzo\n"
        "Mario Rossi;mario@x.it;Via Roma 1\n"   # ok
        "Anna;non-una-email;Via Po 2\n"          # bad email
        ";luca@x.it;Via Po 3\n"                  # missing name
        "Gino;gino@x.it;\n"                      # blank field: left to the signer, not an error
        "Mario Rossi;MARIO@x.it;Via Roma 1\n"    # duplicate of row 2 (email case-insensitive)
    ), "l.csv")
    m = bulk.build_mapping(t.headers, tpl)
    plan = bulk.plan_rows(t, m, tpl)
    states = {r.number: (r.state, r.error) for r in plan.rows}
    assert states[2][0] == bulk.STATE_PENDING
    assert "non valida" in states[3][1]
    assert "nome" in states[4][1]
    assert states[5][0] == bulk.STATE_PENDING
    assert "duplicato della riga 2" in states[6][1]
    s = bulk.summarize(plan, dry_run=True)
    assert s["righe_valide"] == 2 and s["righe_con_errori"] == 3
    assert len(s["errori_principali"]) == 3
    payload = bulk.build_payload(plan.rows[3].data, m, tpl, "o", "t")
    assert "placeholders" not in payload  # blank cell is not sent as an empty value


async def test_same_email_for_two_signers_rejected():
    api = FakeHeuApi(signers=[{"id": "s1", "full_name": "A"}, {"id": "s2", "full_name": "B"}], placeholders=[])
    tpl = await template(api)
    t = bulk.parse_table(csv_bytes("Nome A,Email A,Nome B,Email B\nx,same@x.it,y, Same@X.it\n"), "l.csv")
    plan = bulk.plan_rows(t, bulk.build_mapping(t.headers, tpl), tpl)
    assert "stessa email" in plan.rows[0].error


async def test_error_list_is_capped():
    tpl = await template()
    lines = "".join(f"N{i};bad{i};Via\n" for i in range(40))
    t = bulk.parse_table(csv_bytes("Nome;Email;Indirizzo\n" + lines), "l.csv")
    plan = bulk.plan_rows(t, bulk.build_mapping(t.headers, tpl), tpl)
    s = bulk.summarize(plan, dry_run=True)
    assert len(s["errori_principali"]) == bulk.MAX_ERRORS_LISTED
    assert "20 non elencati" in s["altri_errori"]


# ------------------------------------------------------------------ sending

async def run_send(plan, api, **kw):
    changes = []

    async def on_change(r):
        changes.append((r.number, r.state))

    result = await bulk.send_rows(plan, api, bulk.Throttle(0, sleep=no_sleep), "Oggetto", "Testo", None,
                                  on_change, **kw)
    return result, changes


def three_rows():
    return csv_bytes("Nome;Email;Indirizzo\nA;a@x.it;V1\nB;b@x.it;V2\nC;c@x.it;V3\n")


async def test_send_partial_errors_do_not_stop():
    api = FakeHeuApi(create_script=[(201, None, {}), (400, {"message": "placeholder non valido"}, {})])
    tpl = await template(api)
    t = bulk.parse_table(three_rows(), "l.csv")
    plan = bulk.plan_rows(t, bulk.build_mapping(t.headers, tpl), tpl)
    result, changes = await run_send(plan, api)
    assert result == "completato"
    assert [r.state for r in plan.rows] == [bulk.STATE_SENT, bulk.STATE_FAILED, bulk.STATE_SENT]
    assert "placeholder non valido" in plan.rows[1].error
    # Each row is persisted as in progress before the request is made.
    assert changes[0] == (2, bulk.STATE_IN_PROGRESS)
    assert api.created[0]["document_name"] == "Contratto servizi - A"
    assert api.created[0]["placeholders"] == [{"source_id": "p2", "text_value": "V1"}]


async def test_send_retries_rate_limit_and_server_errors():
    api = FakeHeuApi(create_script=[
        (429, {"message": "Too Many Requests"}, {"Retry-After": "120"}),
        (503, {"message": "down"}, {}),
        (201, None, {}),
    ])
    tpl = await template(api)
    t = bulk.parse_table(csv_bytes("Nome;Email;Indirizzo\nA;a@x.it;V1\n"), "l.csv")
    plan = bulk.plan_rows(t, bulk.build_mapping(t.headers, tpl), tpl)
    throttle = bulk.Throttle(0, sleep=no_sleep)
    await bulk.send_rows(plan, api, throttle, "o", "t", None, _noop)
    assert plan.rows[0].state == bulk.STATE_SENT
    assert sum(1 for c in api.calls if c == ("POST", "/pdfs")) == 3


async def test_unknown_outcome_is_not_resent():
    api = FakeHeuApi(create_script=[(502, {"message": "bad gateway"}, {})] * 4)
    tpl = await template(api)
    t = bulk.parse_table(csv_bytes("Nome;Email;Indirizzo\nA;a@x.it;V1\n"), "l.csv")
    plan = bulk.plan_rows(t, bulk.build_mapping(t.headers, tpl), tpl)
    await bulk.send_rows(plan, api, bulk.Throttle(0, sleep=no_sleep), "o", "t", None, _noop)
    assert plan.rows[0].state == bulk.STATE_TO_VERIFY


async def test_revoked_key_stops_job():
    api = FakeHeuApi(create_script=[(401, {"message": "unauthorized"}, {})])
    tpl = await template(api)
    t = bulk.parse_table(three_rows(), "l.csv")
    plan = bulk.plan_rows(t, bulk.build_mapping(t.headers, tpl), tpl)
    with pytest.raises(bulk.StopJob):
        await bulk.send_rows(plan, api, bulk.Throttle(0, sleep=no_sleep), "o", "t", None, _noop)
    assert [r.state for r in plan.rows] == [bulk.STATE_FAILED, bulk.STATE_PENDING, bulk.STATE_PENDING]


async def test_deadline_returns_partial():
    api = FakeHeuApi()
    tpl = await template(api)
    t = bulk.parse_table(three_rows(), "l.csv")
    plan = bulk.plan_rows(t, bulk.build_mapping(t.headers, tpl), tpl)
    result = await bulk.send_rows(plan, api, bulk.Throttle(0, sleep=no_sleep), "o", "t", None, _noop,
                                  deadline=0.0)
    assert result == "parziale"
    assert api.created == []


async def _noop(_row):
    return None


# ------------------------------------------------------------------- resume

async def test_resume_skips_sent_rows_and_flags_interrupted():
    api = FakeHeuApi()
    tpl = await template(api)
    t = bulk.parse_table(three_rows(), "l.csv")
    m = bulk.build_mapping(t.headers, tpl)
    plan = bulk.plan_rows(t, m, tpl)
    plan.rows[0].state, plan.rows[0].document_id = bulk.STATE_SENT, "77"
    plan.rows[1].state = bulk.STATE_IN_PROGRESS  # crash during the request
    previous = bulk.parse_table(plan.outcome_bytes(), "l.esito.csv")

    again = bulk.plan_rows(t, m, tpl, previous)
    assert [r.state for r in again.rows] == [bulk.STATE_SENT, bulk.STATE_TO_VERIFY, bulk.STATE_PENDING]
    assert again.rows[0].document_id == "77"
    await bulk.send_rows(again, api, bulk.Throttle(0, sleep=no_sleep), "o", "t", None, _noop)
    assert [p["document_name"] for p in api.created] == ["Contratto servizi - C"]


async def test_resubmitting_outcome_file_and_editing_a_failed_row():
    api = FakeHeuApi()
    tpl = await template(api)
    t = bulk.parse_table(three_rows(), "l.csv")
    m = bulk.build_mapping(t.headers, tpl)
    plan = bulk.plan_rows(t, m, tpl)
    plan.rows[0].state, plan.rows[0].document_id = bulk.STATE_SENT, "77"
    plan.rows[1].state, plan.rows[1].error = bulk.STATE_FAILED, "HTTP 400"
    outcome = bulk.parse_table(plan.outcome_bytes(), "l.esito.csv")
    outcome.rows[1]["Email"] = "b.corretta@x.it"  # user fixes the failed row and re-uploads

    again = bulk.plan_rows(outcome, m, tpl)
    assert [r.state for r in again.rows] == [bulk.STATE_SENT, bulk.STATE_PENDING, bulk.STATE_PENDING]
    assert "stato" not in [h for h in again.outcome_headers()[:-3]]


async def test_reconcile_adopts_unambiguous_document():
    api = FakeHeuApi(documents=[{"id": "555", "name": "Contratto servizi - A", "created_at": "2099-01-01T00:00:00Z"}])
    tpl = await template(api)
    t = bulk.parse_table(three_rows(), "l.csv")
    m = bulk.build_mapping(t.headers, tpl)
    plan = bulk.plan_rows(t, m, tpl)
    plan.rows[0].state = bulk.STATE_IN_PROGRESS
    previous = bulk.parse_table(plan.outcome_bytes(), "x.csv")
    again = bulk.plan_rows(t, m, tpl, previous)
    await bulk.reconcile(again, api, bulk.Throttle(0, sleep=no_sleep), since=0)
    assert again.rows[0].state == bulk.STATE_SENT and again.rows[0].document_id == "555"


# ----------------------------------------------------------------- download

DOCS = [
    {"id": "1", "name": "NDA Rossi", "status": "signed", "tags": ["Fornitori"], "created_at": "2026-10-05T10:00:00Z"},
    {"id": "2", "name": "NDA Bianchi", "status": "to_sign", "tags": ["Fornitori"], "created_at": "2026-10-06T10:00:00Z"},
    {"id": "3", "name": "Contratto Verdi", "status": "signed", "tags": ["Clienti"], "created_at": "2026-11-01T10:00:00Z"},
    {"id": "4", "name": "NDA/Neri: 2026", "status": "signed", "tags": [], "created_at": "2026-10-31T23:00:00Z"},
]


def test_select_documents_filters():
    sel, _ = bulk.select_documents(DOCS, bulk.DownloadFilter(folder="fornitori"))
    assert [d["id"] for d in sel] == ["1"]
    sel, _ = bulk.select_documents(DOCS, bulk.DownloadFilter(created_from="2026-10-01", created_to="2026-10-31"))
    assert [d["id"] for d in sel] == ["1", "4"]
    sel, _ = bulk.select_documents(DOCS, bulk.DownloadFilter(status="any", name_contains="nda"))
    assert [d["id"] for d in sel] == ["1", "2", "4"]
    sel, missing = bulk.select_documents(DOCS, bulk.DownloadFilter(document_ids=["3", "99"]))
    assert [d["id"] for d in sel] == ["3"] and missing == ["99"]


def test_readable_name():
    assert bulk.readable_name(DOCS[3]) == "NDA Neri 2026 - 4"


async def test_fetch_bundle_with_reading_copy():
    api = FakeHeuApi()
    api.bundles["1"] = make_bundle("Firmato da Rossi")
    got = await bulk.fetch_bundle(api, bulk.Throttle(0, sleep=no_sleep), DOCS[0], with_copy=True)
    assert got.bundle == api.bundles["1"] and got.error == ""
    from pypdf import PdfReader
    assert len(PdfReader(io.BytesIO(got.copy)).pages) == 2

    missing = await bulk.fetch_bundle(api, bulk.Throttle(0, sleep=no_sleep), DOCS[2], with_copy=True)
    assert missing.bundle is None and "destinatario" in missing.error


def test_merged_copy_requires_audit_trail():
    assert bulk.merged_copy(make_bundle(audit=False)) is None
    assert bulk.merged_copy(b"not a zip") is None


async def test_changing_the_mapping_between_runs_never_resends():
    api = FakeHeuApi()
    tpl = await template(api)
    t = bulk.parse_table(three_rows(), "l.csv")
    first = bulk.plan_rows(t, bulk.build_mapping(t.headers, tpl), tpl)
    first.rows[0].state, first.rows[0].document_id = bulk.STATE_SENT, "77"
    previous = bulk.parse_table(first.outcome_bytes(), "l.esito.csv")
    # Second run with a different document-name pattern: the sent row must still be recognised.
    m2 = bulk.build_mapping(t.headers, tpl, {"nome_documento": "NDA {Nome}"})
    again = bulk.plan_rows(t, m2, tpl, previous)
    assert again.rows[0].state == bulk.STATE_SENT and again.rows[0].document_id == "77"


async def test_editing_a_sent_row_holds_it_for_verification():
    api = FakeHeuApi()
    tpl = await template(api)
    t = bulk.parse_table(three_rows(), "l.csv")
    m = bulk.build_mapping(t.headers, tpl)
    first = bulk.plan_rows(t, m, tpl)
    first.rows[0].state, first.rows[0].document_id = bulk.STATE_SENT, "77"
    previous = bulk.parse_table(first.outcome_bytes(), "l.esito.csv")
    t.rows[0]["Indirizzo"] = "Via Nuova 9"
    again = bulk.plan_rows(t, m, tpl, previous)
    assert again.rows[0].state == bulk.STATE_TO_VERIFY and "77" in again.rows[0].error


async def test_new_row_identical_to_a_sent_one_is_a_duplicate():
    api = FakeHeuApi()
    tpl = await template(api)
    t = bulk.parse_table(csv_bytes("Nome;Email;Indirizzo;stato;document_id\nA;a@x.it;V1;inviato;77\nA;a@x.it;V1;;\n"), "l.csv")
    plan = bulk.plan_rows(t, bulk.build_mapping(t.headers, tpl), tpl)
    assert plan.rows[0].state == bulk.STATE_SENT
    assert plan.rows[1].state == bulk.STATE_INVALID and "duplicato della riga 2" in plan.rows[1].error


async def test_shared_field_label_across_signers_like_real_templates():
    # Mirrors the real test-account template: one text field "Nome" per signer.
    api = FakeHeuApi(
        signers=[{"id": "107328", "full_name": "Firmatario #01"}, {"id": "107329", "full_name": "Firmatario #02"}],
        placeholders=[
            {"id": "a", "signer_id": "107328", "type": "text", "text_label": "Nome", "required": True},
            {"id": "b", "signer_id": "107329", "type": "text", "text_label": "Nome", "required": True},
        ],
    )
    tpl = await template(api)
    # A bare "Nome" column must not fill both signers' fields with the same value:
    # each field takes its own signer's column, and the dry run says so.
    m = bulk.build_mapping(["Nome Firmatario #01", "Email Firmatario #01", "Nome Firmatario #02",
                            "Email Firmatario #02", "Nome"], tpl)
    assert m.fields == {"a": "Nome Firmatario #01", "b": "Nome Firmatario #02"} and m.problems == []
    view = m.describe(tpl)
    assert view["campi_precompilati"] == {"Nome (Firmatario #01)": "Nome Firmatario #01",
                                          "Nome (Firmatario #02)": "Nome Firmatario #02"}
    assert "avvisi" in view and "'Nome' non è stata usata" in view["avvisi"][0]
    # Without signer-specific columns, the fields are left to the signers.
    m = bulk.build_mapping(["Nominativo 1", "Mail 1", "Nominativo 2", "Mail 2", "Nome"], tpl,
                           {"firmatari": {"Firmatario #01": {"nome": "Nominativo 1", "email": "Mail 1"},
                                          "Firmatario #02": {"nome": "Nominativo 2", "email": "Mail 2"}}})
    assert m.fields == {} and m.problems == []
    assert m.describe(tpl)["campi_compilati_dal_firmatario"] == ["Nome (Firmatario #01)", "Nome (Firmatario #02)"]
    # Signer-qualified columns are matched to the right field...
    m = bulk.build_mapping(["Nome Firmatario #01", "Email Firmatario #01", "Nome Firmatario #02",
                            "Email Firmatario #02", "Nome Firmatario #01 ", "Firmatario #02 Nome"], tpl)
    assert m.fields["b"] == "Firmatario #02 Nome"
    # ...and an ambiguous override is refused with a clear message.
    m = bulk.build_mapping(["A", "B", "C", "D", "X"], tpl,
                           {"firmatari": {"Firmatario #01": {"nome": "A", "email": "B"},
                                          "Firmatario #02": {"nome": "C", "email": "D"}},
                            "campi": {"Nome": "X"}})
    assert any("più firmatari" in p for p in m.problems)
    m = bulk.build_mapping(["A", "B", "C", "D", "X"], tpl,
                           {"firmatari": {"Firmatario #01": {"nome": "A", "email": "B"},
                                          "Firmatario #02": {"nome": "C", "email": "D"}},
                            "campi": {"Nome (Firmatario #02)": "X"}})
    assert m.problems == [] and m.fields == {"b": "X"}
