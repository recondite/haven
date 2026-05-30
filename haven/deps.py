"""Shared singletons wired once and imported by routers + services.

Lives in its own module (rather than main.py) so routers can import the
authenticated Gmail client without creating an import cycle with the app.
"""
from haven import config
from haven.sources.gmail_auth import GmailAuth

gmail_auth = GmailAuth(
    client_id=config.GOOGLE_OAUTH_CLIENT_ID or "",
    client_secret=config.GOOGLE_OAUTH_CLIENT_SECRET or "",
    redirect_uri=config.GOOGLE_OAUTH_REDIRECT_URI,
    token_path=config.GMAIL_TOKEN_PATH,
)
