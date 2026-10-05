"""Turn extracted resources into store-ready FHIR R4 with stable IDs and provenance.

Every resource extracted from one Drive file gets an ID derived from the file ID
and its local ID, so re-extracting a file overwrites the same resources instead
of duplicating them. All patient references point at a single `Patient/self`.
"""

import uuid
from typing import Any

PATIENT_ID = "self"
PATIENT_REF = f"Patient/{PATIENT_ID}"
_NAMESPACE = uuid.UUID("6f2b0f8e-2d7c-4e55-9a7e-0c1f4a7d9b31")

# Resource types the extractor may produce. Patient is deliberately absent.
ALLOWED_TYPES = frozenset({
    "AllergyIntolerance",
    "CarePlan",
    "Condition",
    "DiagnosticReport",
    "Encounter",
    "FamilyMemberHistory",
    "Immunization",
    "MedicationRequest",
    "MedicationStatement",
    "Observation",
    "Organization",
    "Practitioner",
    "Procedure",
})


def stable_id(file_id: str, resource_type: str, local_id: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{file_id}/{resource_type}/{local_id}"))


def normalize(
    resources: list[dict[str, Any]],
    file_id: str,
    source_url: str,
    full_urls: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Assign stable IDs, point references at them, and drop what can't be stored.

    `full_urls` maps Bundle entry fullUrls (e.g. "urn:uuid:...") to "Type/local-id"
    for resources that came from an existing FHIR Bundle.
    Returns (resources, warnings).
    """
    warnings: list[str] = []
    kept: list[dict[str, Any]] = []
    id_map: dict[str, str] = {}
    for n, res in enumerate(resources):
        rtype = res.get("resourceType")
        if rtype == "Patient":
            continue  # the single Patient/self stands in for every patient record
        if rtype not in ALLOWED_TYPES:
            warnings.append(f"skipped unsupported resource type {rtype!r}")
            continue
        local = str(res.get("id") or f"r{n}")
        new_id = stable_id(file_id, rtype, local)
        id_map[f"{rtype}/{local}"] = f"{rtype}/{new_id}"
        kept.append({**res, "id": new_id})

    refs = dict(id_map)
    for full_url, local_ref in (full_urls or {}).items():
        if local_ref in id_map:
            refs[full_url] = id_map[local_ref]
        elif local_ref.startswith("Patient/"):
            refs[full_url] = PATIENT_REF

    out = []
    for res in kept:
        res = _rewrite_refs(res, refs, warnings)
        meta = {k: v for k, v in res.get("meta", {}).items() if k not in ("versionId", "lastUpdated")}
        res["meta"] = {**meta, "source": source_url}
        out.append(res)
    return out, warnings


def _rewrite_refs(node: Any, refs: dict[str, str], warnings: list[str]) -> Any:
    if isinstance(node, list):
        return [_rewrite_refs(v, refs, warnings) for v in node]
    if not isinstance(node, dict):
        return node
    node = {k: _rewrite_refs(v, refs, warnings) for k, v in node.items()}
    ref = node.get("reference")
    if isinstance(ref, str):
        if ref in refs:
            node["reference"] = refs[ref]
        elif ref.startswith("Patient/") or "/Patient/" in ref:
            node["reference"] = PATIENT_REF
        elif not ref.startswith("#"):
            # Points outside this file: keep the text so nothing is lost, but drop
            # the link, which the store would reject as dangling.
            warnings.append(f"dropped unresolved reference {ref!r}")
            del node["reference"]
            node.setdefault("display", ref)
    return node


def document_reference(
    file_id: str, name: str, path: str, url: str, mime_type: str,
    summary: str = "", date: str = "",
) -> dict[str, Any]:
    """A DocumentReference pointing back at the source file in Drive."""
    doc: dict[str, Any] = {
        "resourceType": "DocumentReference",
        "id": stable_id(file_id, "DocumentReference", "source"),
        "meta": {"source": url},
        "status": "current",
        "subject": {"reference": PATIENT_REF},
        "description": summary or name,
        "content": [{
            "attachment": {"contentType": mime_type, "url": url, "title": f"{path}/{name}".lstrip("/")}
        }],
    }
    # DocumentReference.date is an instant, so only a full date or dateTime fits.
    if "T" in date:
        doc["date"] = date
    elif len(date) == 10:
        doc["date"] = f"{date}T00:00:00Z"
    return doc


def transaction(resources: list[dict[str, Any]], delete_refs: list[str] = ()) -> dict[str, Any]:
    """A transaction Bundle that upserts `resources` and deletes `delete_refs`."""
    entries: list[dict[str, Any]] = [
        {"request": {"method": "DELETE", "url": ref}} for ref in delete_refs
    ]
    for res in resources:
        ref = f"{res['resourceType']}/{res['id']}"
        entries.append({"fullUrl": ref, "resource": res, "request": {"method": "PUT", "url": ref}})
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def patient() -> dict[str, Any]:
    return {"resourceType": "Patient", "id": PATIENT_ID, "active": True}
