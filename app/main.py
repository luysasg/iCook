"""Web server: chat page, iFood login and the chat API (localhost only)."""

import logging
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, auth

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

STATIC = Path(__file__).parent / "static"
SESSIONS: dict[str, agent.Session] = {}

app = FastAPI(title="Bateu a Vontade")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def session_cookie(request: Request, call_next):
    sid = request.cookies.get("sid")
    if not sid or sid not in SESSIONS:
        sid = secrets.token_urlsafe(24)
        SESSIONS[sid] = agent.Session(sid)
    request.state.sid = sid
    response = await call_next(request)
    response.set_cookie("sid", sid, httponly=True, samesite="lax")
    return response


def _session(request: Request) -> agent.Session:
    return SESSIONS[request.state.sid]


def _payload(s: agent.Session, messages: list[dict]) -> JSONResponse:
    return JSONResponse({"connected": s.connected, "phase": s.phase, "messages": messages})


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/login")
async def login(request: Request) -> RedirectResponse:
    s = _session(request)
    s.oauth_state = secrets.token_urlsafe(24)
    s.pkce_verifier, challenge = auth.new_pkce()
    return RedirectResponse(auth.authorize_url(s.oauth_state, challenge))


@app.get("/callback")
async def callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None,
    error_description: str | None = None,
):
    s = _session(request)
    if error:
        return _error_page(f"O iFood recusou o login: {error_description or error}")
    if not code or not state or state != s.oauth_state or not s.pkce_verifier:
        return _error_page("Login inválido ou expirado. Tente conectar de novo.")
    try:
        s.tokens = await auth.exchange_code(code, s.pkce_verifier)
    except auth.AuthError as e:
        log.warning("token exchange failed: %s", e)
        return _error_page(f"Não foi possível concluir o login: {e}")
    finally:
        s.oauth_state = s.pkce_verifier = None
    # First thing after login: the user picks where they are now.
    async with s.lock:
        await agent.ask_address(s, intro="Conta iFood conectada! ✅ ")
    return RedirectResponse("/")


def _error_page(message: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><body style='font-family:sans-serif;padding:2rem'>"
        f"<p>{message}</p><p><a href='/'>Voltar</a></p></body>",
        status_code=400,
    )


class ChatIn(BaseModel):
    message: str


class ChoiceIn(BaseModel):
    number: int


@app.get("/api/state")
async def state(request: Request) -> JSONResponse:
    s = _session(request)
    return _payload(s, s.messages)


@app.post("/api/chat")
async def chat(request: Request, body: ChatIn) -> JSONResponse:
    s = _session(request)
    text = body.message.strip()[:2000]
    if not text:
        return _payload(s, [])
    async with s.lock:
        return _payload(s, await agent.handle_message(s, text))


@app.post("/api/choose_store")
async def choose_store(request: Request, body: ChoiceIn) -> JSONResponse:
    s = _session(request)
    async with s.lock:
        return _payload(s, await agent.choose_store(s, body.number - 1, from_button=True))


@app.post("/api/choose_address")
async def choose_address(request: Request, body: ChoiceIn) -> JSONResponse:
    s = _session(request)
    async with s.lock:
        return _payload(s, await agent.choose_address(s, body.number - 1, from_button=True))


@app.post("/api/confirm")
async def confirm(request: Request) -> JSONResponse:
    s = _session(request)
    async with s.lock:
        return _payload(s, await agent.confirm_order(s))


@app.post("/api/reset")
async def reset(request: Request) -> JSONResponse:
    old = _session(request)
    s = agent.Session(old.id, tokens=old.tokens)
    SESSIONS[old.id] = s
    if s.connected:
        async with s.lock:
            await agent.ask_address(s)
    return _payload(s, s.messages)


@app.post("/api/logout")
async def logout(request: Request) -> JSONResponse:
    s = _session(request)
    s.tokens = None
    return _payload(s, [])
