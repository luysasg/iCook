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

OPENROUTER_API_KEY = _required("OPENROUTER_API_KEY")
OPENROUTER_URL = os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1")
# Tried in order: on a timeout or any API error (overload, rate limit) the next model answers.
# They must support JSON-schema output ("structured_outputs" on openrouter.ai/models).
LLM_MODELS = [
    m.strip()
    for m in os.getenv(
        "OPENROUTER_MODELS",
        "nvidia/nemotron-3-super-120b-a12b:free,nvidia/nemotron-3.5-super-vl:free,dots-studio/dots-3-note-preview:free",
    ).split(",")
    if m.strip()
]
# Seconds to wait for one model before moving to the next.
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "25"))

# How many grocery stores to compare side by side.
MAX_STORES = int(os.getenv("MAX_STORES", "3"))

# Demo mode: the whole checkout runs against iFood except the final submitOrder.
DRY_RUN = os.getenv("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")
