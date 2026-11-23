"""Test doubles for the HEU API."""
import io
import zipfile

from conftest import make_pdf


class FakeHeuApi:
    """Minimal in-memory HEU API answering the calls the bulk engine makes.

    ``create_script`` lets a test decide, per POST /pdfs call, what the API answers:
    a list of (status, body, headers) tuples consumed in order; once exhausted every
    create succeeds.
    """

    def __init__(self, signers=None, placeholders=None, documents=None, create_script=None):
        self.template = {"id": "tpl1", "name": "Contratto servizi", "type": "template"}
        self.signers = signers if signers is not None else [{"id": "s1", "full_name": "Cliente"}]
        self.placeholders = placeholders if placeholders is not None else [
            {"id": "p1", "signer_id": "s1", "type": "signature", "text_label": None, "required": True},
            {"id": "p2", "signer_id": "s1", "type": "text", "text_label": "Indirizzo", "required": True},
            {"id": "p3", "signer_id": "s1", "type": "text", "text_label": "Note", "required": False},
        ]
        self.documents = documents or []
        self.create_script = list(create_script or [])
        self.created: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.next_id = 1000
        self.bundles: dict[str, bytes] = {}

    async def __call__(self, method, path, **kwargs):
        self.calls.append((method, path))
        if method == "GET" and path == "/pdfs/tpl1":
            return 200, {"data": self.template}, {}
        if method == "GET" and path == "/pdfs/tpl1/signers":
            return 200, {"data": self.signers}, {}
        if method == "GET" and path == "/pdfs/tpl1/placeholders":
            return 200, {"data": self.placeholders}, {}
        if method == "GET" and path == "/pdfs":
            return 200, {"data": self.documents}, {}
        if method == "POST" and path == "/pdfs":
            if self.create_script:
                answer = self.create_script.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                if answer[0] not in (200, 201):
                    return answer
            self.next_id += 1
            payload = kwargs["json"]
            doc = {"id": str(self.next_id), "name": payload["document_name"], "status": "to_sign",
                   "signers": payload["signers"]}
            self.created.append(payload)
            self.documents.append({**doc, "created_at": "2099-01-01T00:00:00Z"})
            return 201, {"message": "ok", "status": 201, "data": doc}, {}
        if method == "GET" and path.endswith("/bundle"):
            doc_id = path.split("/")[2]
            if doc_id in self.bundles:
                return 200, self.bundles[doc_id], {"content-type": "application/zip"}
            return 401, {"message": "not a recipient"}, {}
        return 404, {"message": f"unexpected {method} {path}"}, {}


def make_bundle(text="Contratto firmato", audit=True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("document.pdf", make_pdf([(text, 100, 700)]))
        if audit:
            zf.writestr("audit-trail.pdf", make_pdf([("Audit trail", 100, 700)]))
        zf.writestr("fes/evidence.json", "{}")
    return buf.getvalue()


async def no_sleep(_seconds):
    return None
