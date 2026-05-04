import os
from pathlib import Path

# OAuth flow uses http://127.0.0.1 redirect — oauthlib needs this to allow non-HTTPS.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

import uvicorn
from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(ENV_PATH)


def main() -> None:
    host = os.getenv("HAVEN_HOST", "127.0.0.1")
    port = int(os.getenv("HAVEN_PORT", "8765"))
    reload_dir = str(Path(__file__).parent)
    uvicorn.run(
        "haven.main:app",
        host=host,
        port=port,
        reload=True,
        reload_dirs=[reload_dir],
        log_level="info",
    )


if __name__ == "__main__":
    main()
