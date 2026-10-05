"""Client for a FHIR R4 store in the Google Cloud Healthcare API."""

import json
import logging
import time
from typing import Any

from .fhir import PATIENT_ID, patient
from .http import ApiError, check, request

HEALTHCARE_URL = "https://healthcare.googleapis.com/v1"
FHIR_JSON = "application/fhir+json; charset=utf-8"

log = logging.getLogger(__name__)


class FhirStore:
    def __init__(
        self, session: Any, project: str, location: str, dataset: str, store: str,
        base_url: str = HEALTHCARE_URL, sleep=time.sleep,
    ) -> None:
        self._session = session
        self._sleep = sleep
        self.project, self.location, self.dataset, self.store = project, location, dataset, store
        self._base = base_url.rstrip("/")

    @property
    def dataset_path(self) -> str:
        return f"projects/{self.project}/locations/{self.location}/datasets/{self.dataset}"

    @property
    def store_path(self) -> str:
        return f"{self.dataset_path}/fhirStores/{self.store}"

    @property
    def fhir_url(self) -> str:
        return f"{self._base}/{self.store_path}/fhir"

    def _req(self, method: str, url: str, **kwargs: Any) -> Any:
        return request(self._session, method, url, sleep=self._sleep, **kwargs)

    def ensure(self) -> list[str]:
        """Create the dataset, FHIR store and Patient/self if missing. Returns what was created."""
        created = []
        if self._req("GET", f"{self._base}/{self.dataset_path}").status_code == 404:
            parent = f"projects/{self.project}/locations/{self.location}"
            check(self._req("POST", f"{self._base}/{parent}/datasets",
                            params={"datasetId": self.dataset}, json={}), "create dataset")
            self._wait_for(f"{self._base}/{self.dataset_path}", "dataset")
            created.append(self.dataset_path)
        if self._req("GET", f"{self._base}/{self.store_path}").status_code == 404:
            # enableUpdateCreate lets PUT create resources under our own stable IDs.
            check(self._req("POST", f"{self._base}/{self.dataset_path}/fhirStores",
                            params={"fhirStoreId": self.store},
                            json={"version": "R4", "enableUpdateCreate": True}),
                  "create FHIR store")
            created.append(self.store_path)
        if self._req("GET", f"{self.fhir_url}/Patient/{PATIENT_ID}").status_code == 404:
            self.put(patient())
            created.append(f"Patient/{PATIENT_ID}")
        return created

    def _wait_for(self, url: str, what: str, attempts: int = 30) -> None:
        for _ in range(attempts):
            if self._req("GET", url).status_code < 400:
                return
            self._sleep(2)
        raise ApiError(f"timed out waiting for the {what} to be created")

    def put(self, resource: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.fhir_url}/{resource['resourceType']}/{resource['id']}"
        resp = self._req("PUT", url, data=json.dumps(resource), headers={"Content-Type": FHIR_JSON})
        return check(resp, f"PUT {resource['resourceType']}/{resource['id']}").json()

    def execute(self, bundle: dict[str, Any]) -> dict[str, Any]:
        """Run a transaction Bundle; it applies entirely or not at all."""
        resp = self._req("POST", self.fhir_url, data=json.dumps(bundle),
                         headers={"Content-Type": FHIR_JSON})
        return check(resp, "FHIR transaction").json()
