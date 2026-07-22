"""Centralized configuration loaded from .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
SECRETS_DIR = DATA_DIR / "secrets"
AGENTS_CONFIG_DIR = ROOT / "agents"

# SecondBrain wiki (roster source of truth + knowledge base). Lives alongside
# the Projects/ tree: Documents/Claude/SecondBrain. Overridable for other hosts.
SECONDBRAIN_DIR = Path(os.getenv("HAVEN_SECONDBRAIN_DIR", str(ROOT.parent.parent / "SecondBrain")))

load_dotenv(ROOT / ".env")

# Server
HAVEN_HOST = os.getenv("HAVEN_HOST", "127.0.0.1")
HAVEN_PORT = int(os.getenv("HAVEN_PORT", "8765"))

# Auth — bearer/basic token on every endpoint. If unset, auth is DISABLED
# (localhost-only dev). Set it to enforce. Browser uses Basic (native prompt,
# works with EventSource); API clients use `Authorization: Bearer <token>`.
HAVEN_AUTH_TOKEN = os.getenv("HAVEN_AUTH_TOKEN")

# Send mode — "dry" (default): approving a draft records the action but sends
# nothing. "live": the executor actually posts/sends. Arming is a deliberate
# .env act (GT sign-off 2026-07-19); the UI badge and System view reflect it.
SEND_MODE = os.getenv("HAVEN_SEND_MODE", "dry")

# Quiet hours — no automatic polls fire in this local-time window (manual
# "Poll now" still works). Also used as the wake hour for the "tomorrow" snooze.
QUIET_HOURS_START = int(os.getenv("HAVEN_QUIET_HOURS_START", "0"))   # midnight
QUIET_HOURS_END = int(os.getenv("HAVEN_QUIET_HOURS_END", "7"))       # 7 AM (exclusive)

# Sources Haven knows how to poll/cache. Used to validate {source} path params.
KNOWN_SOURCES = ("gmail", "slack", "freshservice", "otter", "jira", "asana")

# LLM
# LLM_MODE selects the runtime backend: "cli"/"claude" -> Claude CLI shell-out,
# "local" -> OpenAI-compatible local endpoint (Ollama / LM Studio).
LLM_MODE = os.getenv("HAVEN_LLM_MODE", "cli")
LLM_MODEL = os.getenv("HAVEN_LLM_MODEL", "claude-sonnet-4-6")
LLM_MODEL_CHEAP = os.getenv("HAVEN_LLM_MODEL_CHEAP", "claude-haiku-4-5")

# Local LLM backend (used when HAVEN_LLM_MODE=local). Ollama serves an
# OpenAI-compatible API at :11434/v1; LM Studio at :1234/v1.
LOCAL_LLM_BASE_URL = os.getenv("HAVEN_LOCAL_LLM_BASE_URL", "http://localhost:11434/v1")
LOCAL_LLM_MODEL = os.getenv("HAVEN_LOCAL_LLM_MODEL", "llama3.1")

# Scoring concurrency. Local single-GPU engines (LM Studio) 400 on concurrent
# long-context predictions ("Engine protocol predict" error), so serialize on
# local; Claude handles parallelism fine. Override with HAVEN_SCORE_CONCURRENCY.
SCORE_CONCURRENCY = int(os.getenv("HAVEN_SCORE_CONCURRENCY", "1" if LLM_MODE == "local" else "5"))

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

# Asana
ASANA_TOKEN = os.getenv("ASANA_TOKEN")            # Personal Access Token
ASANA_WORKSPACE = os.getenv("ASANA_WORKSPACE")    # optional workspace gid; else first from /users/me

# Otter.ai
OTTER_API_KEY = os.getenv("OTTER_API_KEY")
OTTER_API_BASE = os.getenv("OTTER_API_BASE", "https://api.otter.ai/v1")
