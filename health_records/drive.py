"""Walk a Google Drive folder tree and download the files in it."""

import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .http import check, request

DRIVE_URL = "https://www.googleapis.com/drive/v3"
FOLDER = "application/vnd.google-apps.folder"
# Native Google files have no bytes of their own; they are exported instead.
EXPORTS = {
    "application/vnd.google-apps.document": "application/pdf",
    "application/vnd.google-apps.spreadsheet": "text/csv",  # first sheet only
}
_FIELDS = "nextPageToken, files(id, name, mimeType, modifiedTime, md5Checksum, size)"


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    modified_time: str
    path: str  # folder path within the crawled tree, e.g. "Labs/2024"
    md5: str = ""
    size: int = 0

    @property
    def download_mime_type(self) -> str:
        return EXPORTS.get(self.mime_type, self.mime_type)

    @property
    def url(self) -> str:
        return f"https://drive.google.com/file/d/{self.id}/view"


def folder_id(value: str) -> str:
    """Accept a bare folder ID or a Drive folder URL."""
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", value) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", value)
    return m.group(1) if m else value.strip()


class DriveClient:
    def __init__(self, session: Any, base_url: str = DRIVE_URL, sleep=time.sleep) -> None:
        """`session` is a requests-compatible session that adds auth headers."""
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._sleep = sleep

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{self._base_url}/{path}"
        return check(request(self._session, "GET", url, sleep=self._sleep, params=params), path)

    def children(self, parent_id: str) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {
            "q": f"'{parent_id}' in parents and trashed = false",
            "fields": _FIELDS,
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        while True:
            page = self._get("files", params).json()
            yield from page.get("files", [])
            token = page.get("nextPageToken")
            if not token:
                return
            params["pageToken"] = token

    def walk(self, root_id: str) -> Iterator[DriveFile]:
        """Yield every non-folder file under `root_id`, depth first."""
        stack = [(root_id, "")]
        seen = {root_id}
        while stack:
            parent, path = stack.pop()
            for f in self.children(parent):
                if f["mimeType"] == FOLDER:
                    if f["id"] not in seen:  # shortcuts/multi-parent folders can loop
                        seen.add(f["id"])
                        stack.append((f["id"], f"{path}/{f['name']}".lstrip("/")))
                    continue
                yield DriveFile(
                    id=f["id"],
                    name=f["name"],
                    mime_type=f["mimeType"],
                    modified_time=f.get("modifiedTime", ""),
                    path=path,
                    md5=f.get("md5Checksum", ""),
                    size=int(f.get("size", 0)),
                )

    def download(self, f: DriveFile) -> bytes:
        if f.mime_type in EXPORTS:
            resp = self._get(f"files/{f.id}/export", {"mimeType": EXPORTS[f.mime_type]})
        else:
            resp = self._get(f"files/{f.id}", {"alt": "media", "supportsAllDrives": "true"})
        return resp.content
