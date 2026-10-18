"""Web server: chat page, iFood login and the chat API (localhost only)."""

import html
import json
import logging
import secrets
from collections.abc import Awaitable
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, auth, config

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


def _payload(s: agent.Session) -> JSONResponse:
    # Always the whole conversation: the page renders it by message id, so a reload loses nothing.
    return JSONResponse(
        {
            "connected": s.connected,
            "phase": s.phase,
            "dry_run": config.DRY_RUN,
            "needs_resume": s.needs_resume and s.connected,
            "messages": s.messages,
        }
    )


async def _locked(s: agent.Session, work: Awaitable[object]) -> JSONResponse:
    async with s.lock:
        try:
            await work
        finally:
            s.activity = ""
    return _payload(s)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/login")
async def login(request: Request) -> RedirectResponse:
    """Opened in a popup (or new tab) so the chat tab keeps its context."""
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
        return _auth_result(False, f"O iFood recusou o login: {error_description or error}")
    if not code or not state or state != s.oauth_state or not s.pkce_verifier:
        return _auth_result(False, "Login inválido ou expirado. Tente conectar de novo.")
    try:
        s.tokens = await auth.exchange_code(code, s.pkce_verifier)
    except auth.AuthError as e:
        log.warning("token exchange failed: %s", e)
        return _auth_result(False, f"Não foi possível concluir o login: {e}")
    finally:
        s.oauth_state = s.pkce_verifier = None
    # The chat tab calls /api/resume, which continues whatever was interrupted.
    s.needs_resume = True
    return _auth_result(True, "Conta iFood conectada! Pode fechar esta janela e voltar à conversa.")


def _auth_result(ok: bool, message: str) -> HTMLResponse:
    """Tells the chat tab how the login went (postMessage + BroadcastChannel) and closes the popup."""
    payload = json.dumps({"type": "ifood-auth", "ok": ok, "message": message}).replace("</", "<\\/")
    return HTMLResponse(
        f"""<!doctype html><html lang="pt-BR"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Conectar iFood</title>
<body style="font-family:system-ui,sans-serif;padding:32px 16px;text-align:center;color:#3f3e3e">
<p style="font-size:18px">{"✅" if ok else "⚠️"} {html.escape(message)}</p>
<p><a href="/" style="color:#ea1d2c;font-weight:600">Voltar à conversa</a></p>
<script>
const msg = {payload};
try {{ new BroadcastChannel("ifood-auth").postMessage(msg); }} catch (e) {{}}
if (window.opener) {{
  try {{ window.opener.postMessage(msg, location.origin); }} catch (e) {{}}
  setTimeout(() => window.close(), 400);
}}
</script></body></html>""",
        status_code=200 if ok else 400,
    )


class ChatIn(BaseModel):
    message: str


class ChoiceIn(BaseModel):
    number: int


class ConfirmIn(BaseModel):
    document: str | None = None


@app.get("/api/state")
async def state(request: Request) -> JSONResponse:
    return _payload(_session(request))


@app.get("/api/auth_status")
async def auth_status(request: Request) -> JSONResponse:
    return JSONResponse({"connected": _session(request).connected})


@app.get("/api/activity")
async def activity(request: Request) -> JSONResponse:
    """What the agent is doing right now (no lock: read while a request is running)."""
    return JSONResponse({"activity": _session(request).activity})


@app.post("/api/resume")
async def resume(request: Request) -> JSONResponse:
    s = _session(request)

    async def work() -> None:
        if s.connected and s.needs_resume:
            s.needs_resume = False
            await agent.resume(s)

    return await _locked(s, work())


@app.post("/api/chat")
async def chat(request: Request, body: ChatIn) -> JSONResponse:
    s = _session(request)
    text = body.message.strip()[:2000]
    if not text:
        return _payload(s)
    return await _locked(s, agent.handle_message(s, text))


@app.post("/api/choose_store")
async def choose_store(request: Request, body: ChoiceIn) -> JSONResponse:
    s = _session(request)
    return await _locked(s, agent.choose_store(s, body.number - 1, from_button=True))


@app.post("/api/choose_address")
async def choose_address(request: Request, body: ChoiceIn) -> JSONResponse:
    s = _session(request)
    return await _locked(s, agent.choose_address(s, body.number - 1, from_button=True))


@app.post("/api/choose_payment")
async def choose_payment(request: Request, body: ChoiceIn) -> JSONResponse:
    s = _session(request)
    return await _locked(s, agent.choose_payment(s, body.number - 1, from_button=True))


@app.post("/api/choose_voucher")
async def choose_voucher(request: Request, body: ChoiceIn) -> JSONResponse:
    """number 0 = no coupon."""
    s = _session(request)
    index = body.number - 1 if body.number > 0 else None
    return await _locked(s, agent.choose_voucher(s, index, from_button=True))


@app.post("/api/confirm")
async def confirm(request: Request, body: ConfirmIn) -> JSONResponse:
    s = _session(request)
    return await _locked(s, agent.confirm_order(s, body.document))


@app.post("/api/track")
async def track(request: Request) -> JSONResponse:
    """Automatic polling from the tracker card, at the cadence trackOrder's agentHints asked for."""
    s = _session(request)

    async def work() -> None:
        if s.order_id:
            await agent.track_order(s, refresh=True)

    return await _locked(s, work())


@app.post("/api/reset")
async def reset(request: Request) -> JSONResponse:
    old = _session(request)
    # Keep the login, the getUser profile and the cart bookkeeping (a store's open cart is resumed, not recreated).
    s = agent.Session(old.id, tokens=old.tokens, guard=old.guard, profile=old.profile)
    SESSIONS[old.id] = s
    if s.connected:
        return await _locked(s, agent.new_conversation(s))
    return _payload(s)


@app.post("/api/logout")
async def logout(request: Request) -> JSONResponse:
    s = _session(request)
    s.tokens = None
    return _payload(s)
