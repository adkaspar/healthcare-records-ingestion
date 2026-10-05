# healthcare-records-ingestion

`health-records` crawls a Google Drive folder of your medical records
(lab results, visit summaries, scans, portal exports), extracts the clinical
data as **FHIR R4** resources, and stores them in a FHIR store in the
**Google Cloud Healthcare API**.

```
Drive folder ──crawl──▶ records/files/ ──extract──▶ records/fhir/ ──upload──▶ FHIR store
                         (local copies)   FHIR JSON as-is,       (transaction per file)
                                          everything else via Claude
```

- **FHIR JSON files** (resources or Bundles from a patient portal) are imported
  as-is, with no Claude call.
- **PDFs, scans/photos (JPEG, PNG, WebP, GIF), text, CSV, C-CDA XML, Google Docs
  and Sheets** go to Claude, which returns FHIR resources (Observation,
  Condition, MedicationStatement, Immunization, DiagnosticReport, and so on).
  HEIC photos aren't supported yet. Convert them to JPEG first.
- Every file also gets a `DocumentReference` that links back to it in Drive, and
  every resource's `meta.source` holds the Drive URL.
- All resources reference a single `Patient/self`.
- Runs are incremental. Unchanged files are skipped. When a file changes, it is
  re-extracted, and on upload its old resources are replaced in one transaction.

## Setup (one time)

1. **APIs.** Enable the Drive and Healthcare APIs on the project:
   ```bash
   gcloud services enable drive.googleapis.com healthcare.googleapis.com --project persona-adkaspar
   ```
   The Healthcare API needs a billing account on the project. At personal scale
   it should stay inside the free tier (1 GB of storage and 25,000 requests a
   month).
2. **OAuth.** On the consent screen, set it to *External*, leave it in
   *Testing*, add your own Google account as a test user, and add the scopes
   `drive.readonly` and `cloud-healthcare`. Under *Credentials*, create an OAuth
   client ID of type *Desktop app* and save its JSON as `client_secret.json` in
   this directory. It is gitignored.
3. **Anthropic API key.** Set `export ANTHROPIC_API_KEY=...`. Claude API usage is
   billed separately from a Claude.ai subscription.
4. **Log in and create the store.** `setup` creates the dataset, the FHIR store
   and `Patient/self`, then saves the location to `records/config.json`:
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -e .
   health-records login
   health-records setup --project persona-adkaspar   # --location us-central1 --dataset personal-health --store records
   ```

## Usage

```bash
# Download everything under a folder (ID or URL)
health-records crawl "https://drive.google.com/drive/folders/<id>"

# See what Claude extraction would cost before spending anything (token counting is free)
health-records extract --estimate

# Try a few files first, check the results in records/fhir/, then do the rest
health-records extract --limit 3
health-records extract

health-records upload
health-records status           # counts by status, total Claude spend, and per-file errors

# Later: everything in one go (only new or changed files cost anything)
health-records sync "https://drive.google.com/drive/folders/<id>"
```

`extract` uses `claude-opus-5-5` at `--effort high` by default. To lower the
cost, pass `--model claude-sonnet-5-5` (about half the price) or
`--effort medium`. To skip Claude entirely and import only files that are
already FHIR, pass `--no-claude`. `--retry-failed` re-attempts files that
failed before.

To query the store:
```bash
FHIR=https://healthcare.googleapis.com/v1/projects/persona-adkaspar/locations/us-central1/datasets/personal-health/fhirStores/records/fhir
curl -H "Authorization: Bearer $(gcloud auth print-access-token)" "$FHIR/Observation?code=4548-4"   # HbA1c
```

> While the consent screen is in *Testing*, Google expires refresh tokens after
> 7 days. If a command fails with an auth error, run `health-records login` again.

> **Privacy.** `records/` holds local copies of your records and is gitignored.
> Documents that need extraction are sent to the Anthropic API. Review the
> extracted resources before relying on them. Extraction can miss or misread
> values, and every resource links back to its source document so you can check
> it.

## Development

```bash
pip install -e '.[test]'
pytest
```
