"""Gmail OAuth flow — generates auth URL, handles callback, persists token."""
import os

# When upgrading scopes (e.g. readonly -> modify) Google merges previously-granted
# scopes into the token response. The default oauthlib check rejects that as a
# scope mismatch. Relaxing the check accepts the broader returned scope set.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from pathlib import Path
from typing import Optional

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

# gmail.modify is read + label/state changes. It does NOT grant permanent delete
# (those require gmail.modify+gmail.delete or full https://mail.google.com/). Per
# Haven's ground rules, archive (INBOX label removal) is allowed; delete is not.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class GmailAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        token_path: Path,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.token_path = token_path
        # PKCE: state -> code_verifier captured during begin(), replayed in complete().
        self._pending: dict[str, str] = {}

    def _flow(self, state: str | None = None) -> Flow:
        client_config = {
            "web": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [self.redirect_uri],
            }
        }
        return Flow.from_client_config(
            client_config,
            scopes=SCOPES,
            redirect_uri=self.redirect_uri,
            state=state,
        )

    def begin(self) -> str:
        """Return the Google authorization URL to redirect the user to."""
        flow = self._flow()
        url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",  # ensure refresh token is issued every time
        )
        # google-auth-oauthlib uses PKCE by default — capture the verifier so the
        # callback handler (which constructs a fresh Flow) can replay it.
        self._pending[state] = flow.code_verifier
        return url

    def complete(self, full_callback_url: str, state: str) -> None:
        """Process the redirect from Google, exchange code for tokens, persist."""
        if state not in self._pending:
            raise ValueError("Invalid or expired OAuth state")
        code_verifier = self._pending.pop(state)

        flow = self._flow(state=state)
        flow.code_verifier = code_verifier
        flow.fetch_token(authorization_response=full_callback_url)
        creds = flow.credentials

        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json())

    def is_authed(self) -> bool:
        return self.token_path.exists()

    def credentials(self) -> Optional[Credentials]:
        if not self.is_authed():
            return None
        return Credentials.from_authorized_user_file(str(self.token_path), SCOPES)

    def has_required_scopes(self) -> bool:
        """True if the persisted token covers all SCOPES we now need."""
        creds = self.credentials()
        if creds is None:
            return False
        granted = set(creds.scopes or [])
        return all(s in granted for s in SCOPES)
