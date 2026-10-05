"""The crawl -> extract -> upload pipeline, with resumable per-file state.

Layout under the data directory (gitignored: it holds health data):

    files/<drive-id><ext>   downloaded copies of the Drive files
    fhir/<drive-id>.json    resources extracted from each file, ready to upload
    state.json              per-file status, so each step only does new work
    config.json             FHIR store location, written by `setup`

A file's status moves downloaded -> extracted -> uploaded. When it changes in
Drive it goes back to downloaded, and the next upload replaces its resources.
"""

import json
import logging
import mimetypes
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import fhir
from .drive import DriveClient
from .extract import ClaudeExtractor, ExtractionError, import_fhir, is_supported, needs_claude

log = logging.getLogger(__name__)

DOWNLOADED, EXTRACTED, UPLOADED, SKIPPED, FAILED = (
    "downloaded", "extracted", "uploaded", "skipped", "failed",
)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


class State:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "state.json"
        self.files: dict[str, dict[str, Any]] = (
            json.loads(self.path.read_text())["files"] if self.path.exists() else {}
        )

    def save(self) -> None:
        write_json(self.path, {"files": self.files})

    def with_status(self, *statuses: str) -> Iterator[tuple[str, dict[str, Any]]]:
        for file_id, entry in list(self.files.items()):
            if entry["status"] in statuses:
                yield file_id, entry

    def local_path(self, file_id: str) -> Path:
        return self.root / self.files[file_id]["local"]

    def fhir_path(self, file_id: str) -> Path:
        return self.root / "fhir" / f"{file_id}.json"


def crawl(drive: DriveClient, folder: str, state: State) -> dict[str, int]:
    """Download new or changed files under `folder`."""
    counts = {"new": 0, "changed": 0, "unchanged": 0, "unsupported": 0}
    for f in drive.walk(folder):
        prev = state.files.get(f.id)
        version = f.md5 or f.modified_time
        if prev and prev["version"] == version and (state.root / prev["local"]).exists():
            counts["unchanged"] += 1
            continue
        entry = {
            **(prev or {}),
            "name": f.name, "path": f.path, "url": f.url,
            "mime_type": f.download_mime_type, "version": version,
            "modified_time": f.modified_time, "error": None,
        }
        if not is_supported(f.download_mime_type):
            entry.update(status=SKIPPED, local="", error=f"unsupported type {f.mime_type}")
            state.files[f.id] = entry
            counts["unsupported"] += 1
            continue
        ext = mimetypes.guess_extension(f.download_mime_type) or Path(f.name).suffix
        local = Path("files") / f"{f.id}{ext}"
        (state.root / local).parent.mkdir(parents=True, exist_ok=True)
        (state.root / local).write_bytes(drive.download(f))
        entry.update(status=DOWNLOADED, local=str(local))
        state.files[f.id] = entry
        counts["changed" if prev else "new"] += 1
        log.info("downloaded %s/%s", f.path, f.name)
        state.save()
    state.save()
    return counts


def extract(
    extractor: ClaudeExtractor | None, state: State, limit: int | None = None,
    retry_failed: bool = False,
) -> dict[str, Any]:
    """Extract FHIR resources from downloaded files. With no extractor, only
    files that are already FHIR JSON are processed."""
    statuses = (DOWNLOADED, FAILED) if retry_failed else (DOWNLOADED,)
    totals: dict[str, Any] = {
        "files": 0, "claude_calls": 0, "resources": 0, "failed": 0, "cost_usd": 0.0, "pending": 0,
    }
    for file_id, entry in state.with_status(*statuses):
        if not entry.get("local"):
            continue
        data = state.local_path(file_id).read_bytes()
        claude = needs_claude(data, entry["mime_type"])
        if claude and (extractor is None or (limit is not None and totals["claude_calls"] >= limit)):
            totals["pending"] += 1
            continue
        try:
            result = (
                extractor.extract(data, entry["mime_type"], entry["name"], entry["path"])
                if claude else import_fhir(data, entry["mime_type"])
            )
        except ExtractionError as err:
            log.warning("%s: %s", entry["name"], err)
            entry.update(status=FAILED, error=str(err))
            totals["failed"] += 1
            totals["claude_calls"] += claude
            state.save()
            continue
        resources, warnings = fhir.normalize(result.resources, file_id, entry["url"], result.full_urls)
        resources.append(fhir.document_reference(
            file_id, entry["name"], entry["path"], entry["url"], entry["mime_type"],
            result.summary, result.date,
        ))
        write_json(state.fhir_path(file_id), resources)
        entry.update(status=EXTRACTED, error=None, summary=result.summary,
                     warnings=warnings, usage=result.usage or None)
        totals["files"] += 1
        totals["claude_calls"] += claude
        totals["resources"] += len(resources)
        totals["cost_usd"] += (result.usage or {}).get("cost_usd") or 0
        log.info("extracted %d resources from %s", len(resources), entry["name"])
        state.save()
    return totals


def estimate(extractor: ClaudeExtractor, state: State) -> dict[str, Any]:
    """Count input tokens for every file still waiting on Claude (free; no extraction)."""
    from .extract import PRICES

    files = tokens = 0
    for file_id, entry in state.with_status(DOWNLOADED):
        if not entry.get("local"):
            continue
        data = state.local_path(file_id).read_bytes()
        if not needs_claude(data, entry["mime_type"]):
            continue
        try:
            tokens += extractor.count_tokens(data, entry["mime_type"], entry["name"], entry["path"])
        except ExtractionError as err:
            log.warning("%s: %s", entry["name"], err)
            continue
        files += 1
    price = PRICES.get(extractor.model)
    return {
        "files": files, "input_tokens": tokens,
        "input_cost_usd": round(tokens * price[0] / 1e6, 2) if price else None,
    }


def upload(store: Any, state: State) -> dict[str, int]:
    """Send each extracted file's resources to the store in one transaction,
    deleting resources a previous extraction of the same file produced but this
    one did not."""
    counts = {"files": 0, "resources": 0, "deleted": 0, "failed": 0}
    for file_id, entry in state.with_status(EXTRACTED):
        resources = json.loads(state.fhir_path(file_id).read_text())
        refs = [f"{r['resourceType']}/{r['id']}" for r in resources]
        stale = sorted(set(entry.get("uploaded", [])) - set(refs))
        try:
            store.execute(fhir.transaction(resources, stale))
        except Exception as err:  # an ApiError names the offending resource
            log.warning("%s: %s", entry["name"], err)
            entry["error"] = f"upload: {err}"
            counts["failed"] += 1
            state.save()
            continue
        entry.update(status=UPLOADED, uploaded=refs, error=None)
        counts["files"] += 1
        counts["resources"] += len(resources)
        counts["deleted"] += len(stale)
        state.save()
    return counts


def summary(state: State) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    spent = 0.0
    for entry in state.files.values():
        by_status[entry["status"]] = by_status.get(entry["status"], 0) + 1
        spent += (entry.get("usage") or {}).get("cost_usd") or 0
    return {"files": by_status, "claude_cost_usd": round(spent, 2)}

