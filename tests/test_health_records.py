import json
from types import SimpleNamespace

import pytest

from health_records import fhir, pipeline
from health_records.drive import DriveClient, DriveFile, folder_id
from health_records.extract import ClaudeExtractor, ExtractionError, import_fhir
from health_records.store import FhirStore


class FakeResponse:
    def __init__(self, status: int, body=None, content: bytes = b"", headers=None) -> None:
        self.status_code = status
        self._body = body if body is not None else {}
        self.content = content
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Answers requests from a {(method, url-suffix): [responses]} table."""

    def __init__(self, routes) -> None:
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for (m, suffix), responses in self.routes.items():
            if m == method and url.endswith(suffix):
                return responses.pop(0) if len(responses) > 1 else responses[0]
        raise AssertionError(f"unexpected {method} {url}")


def drive_file(id, name, mime="application/pdf", md5="m1"):
    return {"id": id, "name": name, "mimeType": mime, "modifiedTime": "2026-01-01T00:00:00Z",
            "md5Checksum": md5}


# --- Drive --------------------------------------------------------------------

def test_folder_id_accepts_urls():
    assert folder_id("https://drive.google.com/drive/folders/abc_D-1?usp=sharing") == "abc_D-1"
    assert folder_id("https://drive.google.com/open?id=xyz") == "xyz"
    assert folder_id(" plainid ") == "plainid"


def test_walk_recurses_and_paginates():
    session = FakeSession({})
    pages = {
        "root": [
            {"files": [drive_file("f1", "a.pdf")], "nextPageToken": "t"},
            {"files": [{"id": "sub", "name": "Labs", "mimeType": "application/vnd.google-apps.folder"}]},
        ],
        "sub": [{"files": [drive_file("f2", "b.png", "image/png"),
                           {"id": "root", "name": "loop", "mimeType": "application/vnd.google-apps.folder"}]}],
    }

    def request(method, url, params=None, **kw):
        parent = params["q"].split("'")[1]
        return FakeResponse(200, pages[parent].pop(0))

    session.request = request
    files = list(DriveClient(session, sleep=lambda s: None).walk("root"))
    assert [(f.id, f.path) for f in files] == [("f1", ""), ("f2", "Labs")]


def test_google_docs_are_exported():
    session = FakeSession({("GET", "files/d1/export"): [FakeResponse(200, content=b"%PDF")]})
    client = DriveClient(session)
    f = DriveFile("d1", "Notes", "application/vnd.google-apps.document", "t", "")
    assert client.download(f) == b"%PDF"
    assert session.calls[0][2]["params"] == {"mimeType": "application/pdf"}
    assert f.download_mime_type == "application/pdf"


# --- FHIR normalization -------------------------------------------------------

def test_normalize_assigns_stable_ids_and_rewrites_refs():
    resources = [
        {"resourceType": "Patient", "id": "p"},
        {"resourceType": "Observation", "id": "obs-1", "status": "final",
         "subject": {"reference": "Patient/p"}, "performer": [{"reference": "Organization/org-1"}],
         "meta": {"versionId": "3"}},
        {"resourceType": "Organization", "id": "org-1", "name": "Lab"},
        {"resourceType": "DiagnosticReport", "id": "dr-1",
         "result": [{"reference": "Observation/obs-1"}, {"reference": "Observation/missing"}]},
        {"resourceType": "Basic", "id": "x"},
    ]
    out, warnings = fhir.normalize(resources, "file1", "https://drive/f")
    by_type = {r["resourceType"]: r for r in out}
    assert set(by_type) == {"Observation", "Organization", "DiagnosticReport"}
    obs, org, dr = by_type["Observation"], by_type["Organization"], by_type["DiagnosticReport"]
    assert obs["id"] == fhir.stable_id("file1", "Observation", "obs-1")
    assert obs["subject"] == {"reference": "Patient/self"}
    assert obs["performer"][0]["reference"] == f"Organization/{org['id']}"
    assert obs["meta"] == {"source": "https://drive/f"}
    assert dr["result"][0]["reference"] == f"Observation/{obs['id']}"
    assert dr["result"][1] == {"display": "Observation/missing"}
    assert any("Basic" in w for w in warnings) and any("missing" in w for w in warnings)
    # Re-running gives the same IDs, so uploads overwrite instead of duplicating.
    assert fhir.normalize(resources, "file1", "u")[0][0]["id"] == obs["id"]


def test_import_fhir_bundle_resolves_urn_refs():
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"fullUrl": "urn:uuid:p1", "resource": {"resourceType": "Patient"}},
        {"fullUrl": "urn:uuid:c1", "resource": {"resourceType": "Condition", "id": "c",
                                                "subject": {"reference": "urn:uuid:p1"}}},
        {"fullUrl": "urn:uuid:e1", "resource": {"resourceType": "Encounter", "id": "e",
                                                "reasonReference": [{"reference": "urn:uuid:c1"}]}},
    ]}
    ex = import_fhir(json.dumps(bundle).encode(), "application/json")
    out, _ = fhir.normalize(ex.resources, "f", "u", ex.full_urls)
    cond, enc = out
    assert cond["subject"]["reference"] == "Patient/self"
    assert enc["reasonReference"][0]["reference"] == f"Condition/{cond['id']}"
    assert import_fhir(b'{"not": "fhir"}', "application/json") is None


def test_transaction_deletes_then_puts():
    bundle = fhir.transaction([{"resourceType": "Observation", "id": "a"}], ["Observation/old"])
    assert [e["request"] for e in bundle["entry"]] == [
        {"method": "DELETE", "url": "Observation/old"},
        {"method": "PUT", "url": "Observation/a"},
    ]


# --- Claude extraction --------------------------------------------------------

class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.message


def fake_claude(payload, stop_reason="end_turn"):
    calls = []
    message = SimpleNamespace(
        model="claude-opus-5-5", stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=1000, output_tokens=500),
        content=[SimpleNamespace(type="thinking", thinking=""),
                 SimpleNamespace(type="text", text=json.dumps(payload))],
    )

    def stream(**kwargs):
        calls.append(kwargs)
        return FakeStream(message)

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    return client, calls


def test_claude_extractor_parses_resources_and_cost():
    obs = {"resourceType": "Observation", "id": "obs-1", "status": "final"}
    client, calls = fake_claude({"summary": "CBC", "document_date": "2025-03-01",
                                 "resources": [json.dumps(obs)]})
    ex = ClaudeExtractor(client).extract(b"%PDF-1.7", "application/pdf", "cbc.pdf", "Labs")
    assert ex.resources == [obs] and ex.summary == "CBC" and ex.date == "2025-03-01"
    assert ex.usage["cost_usd"] == pytest.approx(0.014)
    req = calls[0]
    assert req["fallbacks"] == "default"
    assert req["output_config"]["format"]["type"] == "json_schema"
    content = req["messages"][0]["content"]
    assert content[0]["type"] == "document" and "Labs/cbc.pdf" in content[1]["text"]


def test_claude_refusal_is_an_error():
    client, _ = fake_claude({}, stop_reason="refusal")
    with pytest.raises(ExtractionError):
        ClaudeExtractor(client).extract(b"x", "text/plain", "n")


# --- Pipeline -----------------------------------------------------------------

class FakeDrive:
    def __init__(self, files, blobs):
        self.files, self.blobs, self.downloads = files, blobs, 0

    def walk(self, folder):
        return iter(self.files)

    def download(self, f):
        self.downloads += 1
        return self.blobs[f.id]


class FakeStore:
    def __init__(self):
        self.bundles = []

    def execute(self, bundle):
        self.bundles.append(bundle)
        return {}


def test_pipeline_end_to_end(tmp_path):
    obs = {"resourceType": "Observation", "id": "o", "status": "final",
           "subject": {"reference": "Patient/x"}}
    files = [
        DriveFile("pdf1", "labs.pdf", "application/pdf", "t1", "Labs", md5="a"),
        DriveFile("json1", "export.json", "application/json", "t1", "", md5="b"),
        DriveFile("heic1", "photo.heic", "image/heic", "t1", "", md5="c"),
    ]
    drive = FakeDrive(files, {"pdf1": b"%PDF", "json1": json.dumps(obs).encode()})
    state = pipeline.State(tmp_path)

    assert pipeline.crawl(drive, "root", state) == {
        "new": 2, "changed": 0, "unchanged": 0, "unsupported": 1}
    assert state.files["heic1"]["status"] == pipeline.SKIPPED

    # Without Claude only the FHIR JSON file is processed; the PDF waits.
    totals = pipeline.extract(None, state)
    assert totals["files"] == 1 and totals["pending"] == 1

    client, _ = fake_claude({"summary": "s", "document_date": "", "resources": [json.dumps(obs)]})
    totals = pipeline.extract(ClaudeExtractor(client), state)
    assert totals["claude_calls"] == 1 and totals["cost_usd"] > 0

    store = FakeStore()
    assert pipeline.upload(store, state)["files"] == 2
    puts = [e["request"]["url"] for b in store.bundles for e in b["entry"]]
    assert sum(u.startswith("DocumentReference/") for u in puts) == 2

    # Unchanged files are not downloaded again.
    pipeline.crawl(drive, "root", pipeline.State(tmp_path))
    assert drive.downloads == 2

    # A changed file is re-extracted; resources it no longer yields get deleted.
    state = pipeline.State(tmp_path)
    drive.files[0] = DriveFile("pdf1", "labs.pdf", "application/pdf", "t2", "Labs", md5="new")
    assert pipeline.crawl(drive, "root", state)["changed"] == 1
    client, _ = fake_claude({"summary": "s", "document_date": "", "resources": []})
    pipeline.extract(ClaudeExtractor(client), state)
    store = FakeStore()
    assert pipeline.upload(store, state)["deleted"] == 1
    assert pipeline.summary(state)["files"] == {"uploaded": 2, "skipped": 1}


# --- FHIR store ---------------------------------------------------------------

def test_store_ensure_creates_missing_pieces():
    base = "projects/p/locations/l/datasets/d"
    session = FakeSession({
        ("GET", base): [FakeResponse(404), FakeResponse(200)],
        ("POST", "locations/l/datasets"): [FakeResponse(200)],
        ("GET", f"{base}/fhirStores/s"): [FakeResponse(404)],
        ("POST", f"{base}/fhirStores"): [FakeResponse(200)],
        ("GET", "fhir/Patient/self"): [FakeResponse(404)],
        ("PUT", "fhir/Patient/self"): [FakeResponse(201, {"id": "self"})],
    })
    store = FhirStore(session, "p", "l", "d", "s", sleep=lambda s: None)
    assert store.ensure() == [base, f"{base}/fhirStores/s", "Patient/self"]
    create_store = next(c for c in session.calls if c[0] == "POST" and c[1].endswith("fhirStores"))
    assert create_store[2]["json"]["enableUpdateCreate"] is True


def test_store_transaction_error_shows_operation_outcome():
    from health_records.http import ApiError

    outcome = {"resourceType": "OperationOutcome", "issue": [{"diagnostics": "bad code"}]}
    session = FakeSession({("POST", "/fhir"): [FakeResponse(400, outcome)]})
    with pytest.raises(ApiError, match="bad code"):
        FhirStore(session, "p", "l", "d", "s").execute({"resourceType": "Bundle"})
