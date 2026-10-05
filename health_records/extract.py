"""Extract FHIR resources from a downloaded document.

Files that are already FHIR JSON are imported as-is. Everything else (PDFs,
scans, photos, text, C-CDA XML, CSV) is sent to Claude, which returns FHIR R4
resources as structured output.
"""

import base64
import json
from dataclasses import dataclass, field
from typing import Any

from .fhir import ALLOWED_TYPES, PATIENT_REF

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
# USD per million input/output tokens, for the cost estimates printed after a run.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
MAX_BYTES = 30 * 1024 * 1024  # the Messages API caps a request at 32 MB

PDF = "application/pdf"
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
TEXT_TYPES = {
    "text/plain", "text/csv", "text/tab-separated-values", "text/markdown",
    "text/html", "text/xml", "application/xml",
}
JSON_TYPES = {"application/json", "application/fhir+json"}

SYSTEM = f"""\
You extract structured health data from one of the patient's own medical \
documents (lab results, visit summaries, discharge notes, prescriptions, \
imaging reports, vaccination cards, portal exports) and express it as FHIR R4 \
resources for their personal health record.

Rules:
- Allowed resourceType values: {", ".join(sorted(ALLOWED_TYPES))}. Never emit a \
Patient; reference the patient as "{PATIENT_REF}" everywhere (subject, patient, \
beneficiary).
- Give every resource a short local id such as "obs-1" or "org-1", and link \
resources to each other as "Type/local-id" (e.g. a DiagnosticReport's result \
pointing at its Observations, a performer pointing at an Organization).
- Record only what the document states. Do not infer diagnoses, values or dates. \
If the document holds no clinical data (a bill, a letter, a blank form), return \
no resources.
- Every coded concept gets a code.text with the document's own wording. Add \
LOINC, SNOMED CT, RxNorm, ICD-10-CM or CVX codings only when you are confident \
of the exact code.
- Lab and vital results are Observations with valueQuantity and UCUM units \
where possible, referenceRange and interpretation when given, and \
effectiveDateTime set to the collection or measurement date.
- Fill required elements: Observation.status ("final" unless stated), \
Condition.clinicalStatus, MedicationStatement.status, Immunization.status, \
Procedure.status, DiagnosticReport.status, Encounter.status and class, \
AllergyIntolerance.clinicalStatus.
- Dates use FHIR formats (YYYY, YYYY-MM, YYYY-MM-DD, or a full dateTime with a \
timezone). Use the precision the document gives.
- Each element of "resources" is one resource serialized as a JSON string.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One sentence: what the document is, who issued it, when.",
        },
        "document_date": {
            "type": "string",
            "description": "Main date of the document as YYYY-MM-DD, or empty if unknown.",
        },
        "resources": {
            "type": "array",
            "items": {"type": "string"},
            "description": "FHIR R4 resources, each serialized as a JSON string.",
        },
    },
    "required": ["summary", "document_date", "resources"],
    "additionalProperties": False,
}


class ExtractionError(RuntimeError):
    pass


@dataclass
class Extraction:
    resources: list[dict[str, Any]]
    summary: str = ""
    date: str = ""
    full_urls: dict[str, str] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)


def needs_claude(data: bytes, mime_type: str) -> bool:
    return _fhir_json(data, mime_type) is None


def is_supported(mime_type: str) -> bool:
    return mime_type == PDF or mime_type in IMAGE_TYPES | TEXT_TYPES | JSON_TYPES


def import_fhir(data: bytes, mime_type: str) -> Extraction | None:
    """Read a file that is already FHIR JSON (a resource or a Bundle)."""
    doc = _fhir_json(data, mime_type)
    if doc is None:
        return None
    if doc["resourceType"] != "Bundle":
        return Extraction(resources=[doc], summary="FHIR resource import")
    resources, full_urls = [], {}
    for n, entry in enumerate(doc.get("entry", [])):
        res = entry.get("resource")
        if not res:
            continue
        res.setdefault("id", f"e{n}")
        if entry.get("fullUrl"):
            full_urls[entry["fullUrl"]] = f"{res['resourceType']}/{res['id']}"
        resources.append(res)
    return Extraction(resources=resources, summary="FHIR Bundle import", full_urls=full_urls)


def _fhir_json(data: bytes, mime_type: str) -> dict[str, Any] | None:
    if mime_type not in JSON_TYPES:
        return None
    try:
        doc = json.loads(data)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) and "resourceType" in doc else None


def content_blocks(data: bytes, mime_type: str) -> list[dict[str, Any]]:
    if len(data) > MAX_BYTES:
        raise ExtractionError(f"file is {len(data) / 2**20:.0f} MB; the limit is 30 MB")
    if mime_type == PDF:
        return [{"type": "document", "source": {
            "type": "base64", "media_type": PDF, "data": base64.standard_b64encode(data).decode(),
        }}]
    if mime_type in IMAGE_TYPES:
        return [{"type": "image", "source": {
            "type": "base64", "media_type": mime_type, "data": base64.standard_b64encode(data).decode(),
        }}]
    if mime_type in TEXT_TYPES | JSON_TYPES:
        return [{"type": "text", "text": data.decode("utf-8", errors="replace")}]
    raise ExtractionError(f"unsupported file type {mime_type}")


class ClaudeExtractor:
    def __init__(self, client: Any, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT) -> None:
        """`client` is an anthropic.Anthropic()."""
        self._client = client
        self.model = model
        self.effort = effort

    def _messages(self, data: bytes, mime_type: str, name: str, path: str) -> list[dict[str, Any]]:
        where = f"{path}/{name}".lstrip("/")
        prompt = f"Source file: {where}\n\nExtract this document's health data as FHIR R4 resources."
        return [{"role": "user", "content": [*content_blocks(data, mime_type), {"type": "text", "text": prompt}]}]

    def count_tokens(self, data: bytes, mime_type: str, name: str, path: str = "") -> int:
        return self._client.messages.count_tokens(
            model=self.model, system=SYSTEM, messages=self._messages(data, mime_type, name, path),
        ).input_tokens

    def extract(self, data: bytes, mime_type: str, name: str, path: str = "") -> Extraction:
        import anthropic

        try:
            msg = self._request(data, mime_type, name, path)
        except anthropic.BadRequestError as err:  # e.g. an encrypted or corrupt PDF
            raise ExtractionError(f"rejected by the API: {err.message}") from err
        return self._parse(msg)

    def _request(self, data: bytes, mime_type: str, name: str, path: str) -> Any:
        with self._client.beta.messages.stream(
            model=self.model,
            max_tokens=64000,
            system=SYSTEM,
            messages=self._messages(data, mime_type, name, path),
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": SCHEMA},
            },
            # If a safety classifier declines (medical text can trip one), the
            # API retries on Anthropic's recommended fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            return stream.get_final_message()

    def _parse(self, msg: Any) -> Extraction:
        usage = {
            "model": msg.model,
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
        }
        usage["cost_usd"] = cost(msg.model, usage["input_tokens"], usage["output_tokens"])
        if msg.stop_reason == "refusal":
            raise ExtractionError("Claude declined to process this document")
        if msg.stop_reason == "max_tokens":
            raise ExtractionError("output hit max_tokens; the document may be too long for one pass")

        text = "".join(b.text for b in msg.content if b.type == "text")
        try:
            out = json.loads(text)
            resources = [json.loads(r) for r in out["resources"]]
        except (ValueError, KeyError, TypeError) as err:
            raise ExtractionError(f"could not parse Claude's output: {err}") from err
        resources = [r for r in resources if isinstance(r, dict) and "resourceType" in r]
        return Extraction(
            resources=resources, summary=out.get("summary", ""), date=out.get("document_date", ""),
            usage=usage,
        )


def cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    return round((input_tokens * price[0] + output_tokens * price[1]) / 1e6, 4)
