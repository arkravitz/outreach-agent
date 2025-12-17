"""Installed-app OAuth bootstrap; no interactive process needed after authorization."""

import os
from pathlib import Path
import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
]


def save_token(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def authorize(client_file, token_file):
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    save_token(token_file, credentials.to_json())


class GoogleAuth(httpx.Auth):
    def __init__(self, token_path):
        self.token_path = Path(token_path)
        self.credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    def auth_flow(self, request):
        if not self.credentials.valid:
            if not self.credentials.refresh_token:
                raise RuntimeError("Google refresh token missing; run pipeline auth")
            # Google refresh transport has its own bounded timeout.
            self.credentials.refresh(Request())
            save_token(self.token_path, self.credentials.to_json())
        request.headers["Authorization"] = f"Bearer {self.credentials.token}"
        yield request


def google_client(token_path):
    return httpx.Client(auth=GoogleAuth(token_path), timeout=httpx.Timeout(20, connect=5))
