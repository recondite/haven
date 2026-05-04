"""Centralized configuration loaded from .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
SECRETS_DIR = DATA_DIR / "secrets"
AGENTS_CONFIG_DIR = ROOT / "agents"

load_dotenv(ROOT / ".env")

# Server
HAVEN_HOST = os.getenv("HAVEN_HOST", "127.0.0.1")
HAVEN_PORT = int(os.getenv("HAVEN_PORT", "8765"))

# LLM
LLM_MODE = os.getenv("HAVEN_LLM_MODE", "cli")
LLM_MODEL = os.getenv("HAVEN_LLM_MODEL", "claude-sonnet-4-6")
LLM_MODEL_CHEAP = os.getenv("HAVEN_LLM_MODEL_CHEAP", "claude-haiku-4-5")

# Google / Gmail
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
GOOGLE_OAUTH_REDIRECT_URI = os.getenv(
    "GOOGLE_OAUTH_REDIRECT_URI", "http://127.0.0.1:8765/oauth/callback"
)
GOOGLE_PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID")
GMAIL_TOKEN_PATH = SECRETS_DIR / "gmail-token.json"

# Linear
LINEAR_API_KEY = os.getenv("LINEAR_API_KEY")
LINEAR_PROJECT_ID = os.getenv("LINEAR_PROJECT_ID")

# Slack
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_USER_TOKEN = os.getenv("SLACK_USER_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

# JIRA
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

# Freshservice
FRESHSERVICE_DOMAIN = os.getenv("FRESHSERVICE_DOMAIN")
FRESHSERVICE_API_KEY = os.getenv("FRESHSERVICE_API_KEY")
