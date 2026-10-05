"""OAuth 2.0 login and cached Google credentials."""

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials


def login(client_secret: Path, token_path: Path, scopes: list[str], port: int = 0) -> Credentials:
    """Run the browser-based installed-app flow and cache the resulting token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), scopes=scopes)
    # access_type=offline + prompt=consent ensures we get a refresh token.
    creds = flow.run_local_server(
        port=port, access_type="offline", prompt="consent", open_browser=True
    )
    save(creds, token_path)
    return creds


def load(token_path: Path, login_command: str = "health-records login") -> Credentials:
    """Load cached credentials, refreshing the access token if it has expired."""
    if not token_path.exists():
        raise FileNotFoundError(f"No token at {token_path}. Run `{login_command}` first.")
    creds = Credentials.from_authorized_user_file(str(token_path))
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        save(creds, token_path)
    return creds


def save(creds: Credentials, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    token_path.chmod(0o600)
