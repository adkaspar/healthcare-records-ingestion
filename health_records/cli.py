"""Command-line entry point: `health-records`."""

import argparse
import json
import logging
import sys
from pathlib import Path

from . import auth

from . import pipeline
from .drive import DriveClient, folder_id
from .extract import DEFAULT_EFFORT, DEFAULT_MODEL, ClaudeExtractor
from .http import ApiError
from .store import FhirStore

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/cloud-healthcare",
]
LOGIN = "health-records login"


def _session(args: argparse.Namespace):
    from google.auth.transport.requests import AuthorizedSession

    return AuthorizedSession(auth.load(args.token, LOGIN))


def _extractor(args: argparse.Namespace) -> ClaudeExtractor:
    import anthropic

    return ClaudeExtractor(anthropic.Anthropic(), model=args.model, effort=args.effort)


def _store(args: argparse.Namespace) -> FhirStore:
    path = args.data / "config.json"
    if not path.exists():
        raise FileNotFoundError(f"No store configured in {path}. Run `health-records setup` first.")
    cfg = json.loads(path.read_text())
    return FhirStore(_session(args), cfg["project"], cfg["location"], cfg["dataset"], cfg["store"])


def _print(counts: dict) -> None:
    for key, value in counts.items():
        print(f"{key:16} {value}")


def cmd_login(args: argparse.Namespace) -> None:
    auth.login(args.client_secret, args.token, SCOPES, port=args.port)
    print(f"Saved token to {args.token}")


def cmd_setup(args: argparse.Namespace) -> None:
    cfg = {"project": args.project, "location": args.location,
           "dataset": args.dataset, "store": args.store}
    store = FhirStore(_session(args), **cfg)
    for path in store.ensure():
        print(f"created {path}")
    pipeline.write_json(args.data / "config.json", cfg)
    print(f"FHIR endpoint: {store.fhir_url}")


def cmd_crawl(args: argparse.Namespace) -> None:
    drive = DriveClient(_session(args))
    _print(pipeline.crawl(drive, folder_id(args.folder), pipeline.State(args.data)))


def cmd_extract(args: argparse.Namespace) -> None:
    state = pipeline.State(args.data)
    if args.estimate:
        _print(pipeline.estimate(_extractor(args), state))
        print("(input only; output and thinking typically add 20-50% more)")
        return
    extractor = None if args.no_claude else _extractor(args)
    totals = pipeline.extract(extractor, state, limit=args.limit, retry_failed=args.retry_failed)
    totals["cost_usd"] = round(totals["cost_usd"], 2)
    _print(totals)


def cmd_upload(args: argparse.Namespace) -> None:
    _print(pipeline.upload(_store(args), pipeline.State(args.data)))


def cmd_sync(args: argparse.Namespace) -> None:
    cmd_crawl(args)
    cmd_extract(args)
    if not args.estimate:
        cmd_upload(args)


def cmd_status(args: argparse.Namespace) -> None:
    state = pipeline.State(args.data)
    print(json.dumps(pipeline.summary(state), indent=2))
    for entry in state.files.values():
        if entry.get("error"):
            where = f"{entry['path']}/{entry['name']}".lstrip("/")
            print(f"  {where}: {entry['error']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="health-records",
        description="Crawl medical records in Google Drive into a FHIR R4 store.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--token", type=Path, default=Path("token-records.json"),
                        help="cached OAuth token (default: token-records.json)")
    parser.add_argument("--data", type=Path, default=Path("records"),
                        help="local working directory (default: records/)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="authorize Drive (read-only) and Cloud Healthcare access")
    p.add_argument("--client-secret", type=Path, default=Path("client_secret.json"))
    p.add_argument("--port", type=int, default=0, help="local redirect port (default: random)")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("setup", help="create the Healthcare dataset, FHIR store and Patient")
    p.add_argument("--project", required=True, help="GCP project ID")
    p.add_argument("--location", default="us-central1")
    p.add_argument("--dataset", default="personal-health")
    p.add_argument("--store", default="records")
    p.set_defaults(func=cmd_setup)

    def add_extract_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--model", default=DEFAULT_MODEL)
        p.add_argument("--effort", default=DEFAULT_EFFORT,
                       choices=["low", "medium", "high", "xhigh", "max"])
        p.add_argument("--limit", type=int, help="send at most N files to Claude this run")
        p.add_argument("--no-claude", action="store_true",
                       help="only import files that are already FHIR JSON")
        p.add_argument("--retry-failed", action="store_true")
        p.add_argument("--estimate", action="store_true",
                       help="count input tokens and estimate cost without extracting")

    p = sub.add_parser("crawl", help="download new/changed files from a Drive folder")
    p.add_argument("folder", help="Drive folder ID or URL")
    p.set_defaults(func=cmd_crawl)

    p = sub.add_parser("extract", help="turn downloaded files into FHIR resources")
    add_extract_args(p)
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("upload", help="send extracted resources to the FHIR store")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("sync", help="crawl, extract and upload in one go")
    p.add_argument("folder", help="Drive folder ID or URL")
    add_extract_args(p)
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("status", help="show per-status file counts, spend and errors")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    import anthropic

    try:
        args.func(args)
    except (ApiError, anthropic.APIError, ValueError, FileNotFoundError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
