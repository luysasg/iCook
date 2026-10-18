"""Settings loaded from the environment (.env)."""

import os

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing {name} in .env (see .env.example)")
    return value


IFOOD_CLIENT_ID = _required("IFOOD_CLIENT_ID")
IFOOD_CLIENT_SECRET = os.getenv("IFOOD_CLIENT_SECRET", "").strip() or None
IFOOD_MCP_URL = os.getenv("IFOOD_MCP_URL", "https://consumer-mcp.ifood.com.br/marketplace")
IFOOD_AUTH_URL = os.getenv("IFOOD_AUTH_URL", "https://id.ifood.com.br")
# The client is not allowed to request any of the documented scopes; empty = server defaults.
IFOOD_SCOPE = os.getenv("IFOOD_SCOPE", "").strip()

APP_PORT = int(os.getenv("APP_PORT", "8000"))
# The OAuth client only accepts loopback redirects on the /callback path.
REDIRECT_URI = f"http://localhost:{APP_PORT}/callback"

GEMINI_API_KEY = _required("GEMINI_API_KEY")
# Tried in order: on a timeout or any API error (overload, rate limit) the next model answers.
GEMINI_MODELS = [
    m.strip()
    for m in os.getenv("GEMINI_MODELS", "gemini-flash-latest,gemini-3.7-flash,gemini-flash-lite-latest").split(",")
    if m.strip()
]
# Seconds to wait for one model before moving to the next.
GEMINI_TIMEOUT = float(os.getenv("GEMINI_TIMEOUT", "25"))

# How many grocery stores to compare side by side.
MAX_STORES = int(os.getenv("MAX_STORES", "3"))
