"""iFood account linking: OAuth 2.1 authorization code + PKCE against id.ifood.com.br.

The authorization server only advertises the authorization_code and refresh_token
grants (no device/QR flow), and the client only accepts loopback redirect URIs.
"""

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from . import config

REFRESH_MARGIN_SECONDS = 300
TOKEN_REQUEST_TIMEOUT = 20
# Used when the token response has no expires_in.
DEFAULT_TOKEN_LIFETIME_SECONDS = 3600


class AuthError(Exception):
    pass


@dataclass
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: float

    def expiring_soon(self) -> bool:
        return time.time() > self.expires_at - REFRESH_MARGIN_SECONDS


def new_pkce() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(state: str, code_challenge: str) -> str:
    params = {
        "client_id": config.IFOOD_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": config.REDIRECT_URI,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": config.IFOOD_MCP_URL,
    }
    if config.IFOOD_SCOPE:
        params["scope"] = config.IFOOD_SCOPE
    return f"{config.IFOOD_AUTH_URL}/authorize?{urlencode(params)}"


async def _token_request(data: dict[str, str]) -> Tokens:
    data = {**data, "client_id": config.IFOOD_CLIENT_ID, "resource": config.IFOOD_MCP_URL}
    if config.IFOOD_CLIENT_SECRET:
        data["client_secret"] = config.IFOOD_CLIENT_SECRET
    async with httpx.AsyncClient(timeout=TOKEN_REQUEST_TIMEOUT) as http:
        response = await http.post(f"{config.IFOOD_AUTH_URL}/token", data=data, headers={"Accept": "application/json"})
    if response.status_code != 200:
        raise AuthError(f"token endpoint returned {response.status_code}: {response.text[:500]}")
    body = response.json()
    return Tokens(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_at=time.time() + int(body.get("expires_in", DEFAULT_TOKEN_LIFETIME_SECONDS)),
    )


async def exchange_code(code: str, code_verifier: str) -> Tokens:
    return await _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.REDIRECT_URI,
            "code_verifier": code_verifier,
        }
    )


async def refresh(tokens: Tokens) -> Tokens:
    if not tokens.refresh_token:
        raise AuthError("no refresh token")
    new = await _token_request({"grant_type": "refresh_token", "refresh_token": tokens.refresh_token})
    if not new.refresh_token:
        new.refresh_token = tokens.refresh_token
    return new
