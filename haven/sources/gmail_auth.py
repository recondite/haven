"""Gmail OAuth flow — generates auth URL, handles callback, persists token."""
import asyncio
import json
import os
import tempfile

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
# gmail.send (added with GT's explicit sign-off, 2026-07-19) lets the executor
# send GT-approved reply drafts. Send-only: it grants no read or delete beyond
# what gmail.modify already covers. Existing tokens lack it — /oauth/authorize
# must be re-run once; /api/auth/gmail/status surfaces scopes_ok until then.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


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
        # Guards token refresh + persistence so concurrent fetchers don't each
        # refresh and race-write the token file. Also gates the cached service.
        self._refresh_lock = asyncio.Lock()
        self._service = None
        self._creds: Optional[Credentials] = None

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

        self._persist_token(creds.to_json())
        # Invalidate caches so the next get_service() picks up the new token.
        self._creds = None
        self._service = None

    def _persist_token(self, token_json: str) -> None:
        """Write the token file atomically so concurrent refreshes can't corrupt it.

        Writes to a temp file in the same directory and os.replace()s it into place
        (atomic on POSIX and Windows for same-volume renames).
        """
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.token_path.parent), prefix=".gmail-token-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(token_json)
            os.replace(tmp, self.token_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    async def get_service(self):
        """Return a cached, authorized Gmail API service, refreshing the token if
        needed. Returns None if Gmail isn't authorized yet.

        Serializes refresh + persistence under a lock so parallel pollers don't
        each refresh and race-write the token file. The built service is cached on
        the auth object and shared across every GmailFetcher, avoiding the repeated
        discovery-client construction the old per-call `_service()` paid.
        """
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        async with self._refresh_lock:
            if self._creds is None:
                self._creds = self.credentials()
            creds = self._creds
            if creds is None:
                return None
            if creds.expired and creds.refresh_token:
                await asyncio.to_thread(creds.refresh, Request())
                self._persist_token(creds.to_json())
                self._service = None  # rebuild against refreshed creds
            if self._service is None:
                self._service = build(
                    "gmail", "v1", credentials=creds, cache_discovery=False
                )
            return self._service

    def new_http(self, timeout: int = 30):
        """Return a fresh AuthorizedHttp wrapping a brand-new httplib2.Http.

        httplib2.Http is NOT thread-safe — it keeps a single TLS socket per host —
        so the cached service's shared http cannot be used from the concurrent
        `asyncio.to_thread` workers in the poll pipeline (Pass A conc=10, Pass C
        conc=5). Two threads writing the same socket corrupt the TLS stream, which
        surfaces as "[SSL] record layer failure" and read timeouts. Each threaded
        `.execute()` must therefore pass its own http from this method.

        Relies on get_service() having already loaded/refreshed creds under the
        async lock, so this stays synchronous and safe to call inside a worker
        thread.
        """
        import httplib2

        from google_auth_httplib2 import AuthorizedHttp

        if self._creds is None:
            self._creds = self.credentials()
        if self._creds is None:
            raise RuntimeError("Gmail not authorized — connect Gmail first")
        return AuthorizedHttp(self._creds, http=httplib2.Http(timeout=timeout))

    def is_authed(self) -> bool:
        return self.token_path.exists()

    def credentials(self) -> Optional[Credentials]:
        if not self.is_authed():
            return None
        return Credentials.from_authorized_user_file(str(self.token_path), SCOPES)

    def has_required_scopes(self) -> bool:
        """True if the persisted token covers all SCOPES we now need.

        Reads the token file's own "scopes" field — Credentials.from_authorized
        _user_file(path, SCOPES) sets .scopes to the REQUESTED list, so checking
        creds.scopes against SCOPES was a tautology (always True).
        """
        if not self.is_authed():
            return False
        try:
            granted = set(json.loads(self.token_path.read_text(encoding="utf-8")).get("scopes") or [])
        except Exception:
            return False
        return all(s in granted for s in SCOPES)
