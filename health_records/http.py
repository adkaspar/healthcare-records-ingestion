"""HTTP helpers shared by the Drive and Cloud Healthcare clients."""

import logging
import time
from typing import Any

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 6

log = logging.getLogger(__name__)


class ApiError(RuntimeError):
    pass


def request(session: Any, method: str, url: str, sleep=time.sleep, **kwargs: Any) -> Any:
    """Send a request, retrying 429/5xx with backoff. Returns the final response
    whatever its status; use `check` to turn errors into ApiError."""
    kwargs.setdefault("timeout", 120)
    for attempt in range(MAX_RETRIES + 1):
        resp = session.request(method, url, **kwargs)
        if resp.status_code not in RETRY_STATUSES or attempt == MAX_RETRIES:
            return resp
        delay = _retry_after(resp) or min(2**attempt, 60)
        log.warning("HTTP %s on %s %s, retrying in %ss", resp.status_code, method, url, delay)
        sleep(delay)
    raise AssertionError("unreachable")


def check(resp: Any, what: str) -> Any:
    if resp.status_code >= 400:
        raise ApiError(f"HTTP {resp.status_code} for {what}: {error_detail(resp)}")
    return resp


def error_detail(resp: Any) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    if not isinstance(body, dict):
        return str(body)[:500]
    # FHIR servers answer with an OperationOutcome.
    if body.get("resourceType") == "OperationOutcome":
        return "; ".join(
            i.get("diagnostics") or i.get("details", {}).get("text", "") or i.get("code", "")
            for i in body.get("issue", [])
        )
    err = body.get("error")
    if isinstance(err, dict):
        return f"{err.get('status', '')} {err.get('message', '')}".strip()
    return str(err or body)[:500]


def _retry_after(resp: Any) -> float | None:
    value = resp.headers.get("Retry-After")
    try:
        return float(value) if value else None
    except ValueError:
        return None
