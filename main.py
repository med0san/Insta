"""Serveur FastAPI : API REST, logs temps réel (WebSocket) et frontend statique."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import os

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import access
import db
import instagram as ig
from auth import auth
from analyzer import analyze_caption, api_status, ollama_status
from bot import bot, build_plan
from priority import clean_keywords, score_result
from logbus import bus

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    bus.log("Serveur démarré.", "success")
    asyncio.create_task(auth.refresh())
    if access.REMOTE:
        found = access.addresses()
        bus.log("Accès réseau activé (mot de passe requis).", "warning")
        for ip in found["tailscale"]:
            bus.log(f"Sur le téléphone (Tailscale, marche en 4G) : {ip}:8000", "warning")
        for ip in found["lan"]:
            bus.log(f"Sur le téléphone (même Wi-Fi seulement) : {ip}:8000", "warning")
    cfg = await db.get_config()
    if cfg.get("bot_enabled"):       # le bot tournait avant l'arrêt / le plantage du serveur : on le relance
        asyncio.create_task(_resume_bot())
    yield
    if bot.running:
        await bot.stop()
    await ig.browser.release()


async def _resume_bot() -> None:
    await asyncio.sleep(3)
    bus.log("Reprise automatique du bot (il tournait avant le redémarrage du serveur).", "warning")
    res = await bot.start()
    if not res["ok"]:
        bus.log(f"Reprise automatique impossible : {res['message']}", "error")


app = FastAPI(title="Insta Giveaway Bot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

OPEN_PATHS = ("/login", "/access/login")


@app.middleware("http")
async def require_password(request: Request, call_next):
    """Si un mot de passe est défini, tout est protégé sauf la page de connexion."""
    if access.enabled() and request.url.path not in OPEN_PATHS:
        if not access.is_authed(request.cookies.get(access.COOKIE)):
            if request.url.path.startswith("/api"):
                return JSONResponse({"detail": "Authentification requise."}, status_code=401)
            return HTMLResponse(access.LOGIN_HTML, status_code=401)
    return await call_next(request)


@app.get("/login")
async def login_page():
    return HTMLResponse(access.LOGIN_HTML)


class AccessIn(BaseModel):
    password: str = Field(max_length=200)


@app.post("/access/login")
async def access_login(body: AccessIn, request: Request):
    ip = request.client.host if request.client else "?"
    if access.locked(ip):
        raise HTTPException(429, "Trop d'essais. Réessayez dans 15 minutes.")
    token = access.check_password(body.password, ip)
    if not token:
        raise HTTPException(401, "Mot de passe incorrect.")
    res = JSONResponse({"ok": True})
    res.set_cookie(access.COOKIE, token, max_age=90 * 86400, httponly=True, samesite="lax")
    return res


# ---------------------------------------------------------------------------
# Modèles
# ---------------------------------------------------------------------------
class ConfigIn(BaseModel):
    friends_to_tag: list[str] | None = None
    hashtags: list[str] | None = None
    daily_quota: int | None = Field(default=None, ge=0, le=1_000_000)
    dry_run: bool | None = None
    headless: bool | None = None
    posts_per_hashtag: int | None = Field(default=None, ge=1, le=200)
    block_cooldown_min: int | None = Field(default=None, ge=10, le=1440)
    max_follows_per_hour: int | None = Field(default=None, ge=0, le=500)
    max_follows_per_day: int | None = Field(default=None, ge=0, le=5000)
    follow_block_cooldown_h: int | None = Field(default=None, ge=1, le=72)
    priority_mode: Literal["off", "deadline", "balanced", "gaming"] | None = None
    priority_keywords: list[str] | None = None
    do_story: bool | None = None
    force_participate: bool | None = None
    speed: Literal["human", "fast"] | None = None
    analyzer_mode: Literal["rules", "ollama", "api"] | None = None
    api_base_url: str | None = Field(default=None, max_length=300)
    api_model: str | None = Field(default=None, max_length=120)
    api_key: str | None = Field(default=None, max_length=300)
    ollama_model: str | None = Field(default=None, max_length=100)
    ollama_url: str | None = Field(default=None, max_length=200)
    default_answer: str | None = Field(default=None, max_length=200)


class LoginIn(BaseModel):
    username: str = ""
    password: str = ""
    remember: bool = False


class AnalyzeIn(BaseModel):
    caption: str = Field(min_length=1, max_length=5000)
    author: str | None = Field(default=None, max_length=60)
    post_date: str | None = Field(default=None, max_length=40)


class TwoFAIn(BaseModel):
    code: str


def _clean_handles(values: list[str], prefix: str) -> list[str]:
    out: list[str] = []
    for v in values:
        v = v.strip().lstrip(prefix).strip()
        if v and v.lower() not in {x.lower() for x in out}:
            out.append(v)
    return out


def _public_config(cfg: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(cfg)
    cfg["has_saved_password"] = bool(cfg.pop("ig_password", ""))
    cfg["has_api_key"] = bool(cfg.pop("api_key", ""))
    return cfg


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
async def status():
    cfg = await db.get_config()
    mode_status = ("Simulé",) if cfg["dry_run"] else ("Participé", "Partiel")
    return {
        "bot": bot.snapshot(),
        "session": auth.snapshot(),
        "today": await db.count_today(mode_status),
        "today_real": await db.count_today(("Participé", "Partiel")),
        "today_simulated": await db.count_today("Simulé"),
        "quota": cfg["daily_quota"],
        "dry_run": cfg["dry_run"],
        "browser_busy": ig.browser.busy,
        "browser_owner": ig.browser.owner,
    }


@app.get("/api/config")
async def get_config():
    return _public_config(await db.get_config())


@app.put("/api/config")
async def put_config(body: ConfigIn):
    values = body.model_dump(exclude_none=True)
    if "friends_to_tag" in values:
        values["friends_to_tag"] = _clean_handles(values["friends_to_tag"], "@")
    if "priority_keywords" in values:
        values["priority_keywords"] = clean_keywords(values["priority_keywords"])
    if "hashtags" in values:
        values["hashtags"] = _clean_handles(values["hashtags"], "#")
    cfg = await db.set_config(values)
    bus.notify("status")
    return _public_config(cfg)


@app.get("/api/analyzer/status")
async def analyzer_status():
    cfg = await db.get_config()
    if cfg["analyzer_mode"] == "api":
        return await api_status(cfg)
    if cfg["analyzer_mode"] == "ollama":
        return await asyncio.to_thread(ollama_status, cfg["ollama_url"], cfg["ollama_model"])
    return {"ok": True, "message": "Analyse par règles (gratuite, hors ligne)."}


@app.post("/api/analyze")
async def analyze_preview(body: AnalyzeIn):
    """Analyse une légende collée par l'utilisateur et montre exactement ce que le bot ferait (rien n'est exécuté)."""
    cfg = await db.get_config()
    author = (body.author or "").strip().lstrip("@") or None
    result = await analyze_caption(body.caption, author, body.post_date, None, cfg)
    plan = build_plan(result, cfg) if result["is_giveaway"] and result["is_active"] else None
    public = {k: v for k, v in result.items() if not k.startswith("_")}
    score = None
    if plan is not None:
        pts, why = score_result(result, body.caption, cfg)
        score = {"score": pts, "why": why, "mode": cfg.get("priority_mode", "balanced")}
    return {"result": public, "plan": plan, "mode": cfg.get("analyzer_mode", "rules"), "priority": score}


@app.post("/api/session/check")
async def session_check():
    return await auth.refresh()


@app.post("/api/login")
async def login(body: LoginIn):
    cfg = await db.get_config()
    username = body.username.strip().lstrip("@") or cfg.get("ig_username", "")
    password = body.password or cfg.get("ig_password", "")
    if not username or not password:
        raise HTTPException(400, "Identifiant et mot de passe requis.")
    if bot.running:
        raise HTTPException(409, "Arrêtez le bot avant de vous reconnecter.")
    await db.set_config(
        {"ig_username": username, **({"ig_password": body.password} if body.remember and body.password else {})}
    )
    if not body.remember and body.password:
        await db.set_config({"ig_password": ""})
    try:
        return await auth.login(username, password, headless=bool(cfg.get("headless", True)))
    except ig.BrowserBusy as e:
        raise HTTPException(409, str(e))


@app.post("/api/login/2fa")
async def login_2fa(body: TwoFAIn):
    return await auth.submit_2fa(body.code)


@app.post("/api/login/manual")
async def login_manual():
    if bot.running:
        raise HTTPException(409, "Arrêtez le bot avant de vous reconnecter.")
    try:
        return await auth.start_manual()
    except ig.BrowserBusy as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        msg = str(e)
        if "display" in msg.lower() or "headed" in msg.lower():
            msg = "Impossible d'ouvrir une fenêtre Chromium (aucun écran disponible sur cette machine)."
        bus.log(f"Connexion manuelle impossible : {msg}", "error")
        raise HTTPException(500, msg)


@app.post("/api/logout")
async def logout():
    try:
        await auth.logout()
    except ig.BrowserBusy as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/bot/start")
async def bot_start():
    res = await bot.start()
    if not res["ok"]:
        raise HTTPException(409, res["message"])
    await db.set_config({"bot_enabled": True})
    return res


@app.post("/api/bot/stop")
async def bot_stop():
    await db.set_config({"bot_enabled": False})   # arrêt voulu : pas de reprise automatique
    return await bot.stop()


@app.get("/api/history")
async def history(limit: int = 300):
    return await db.list_history(min(max(limit, 1), 2000))


@app.delete("/api/history")
async def history_clear():
    if bot.running:
        raise HTTPException(409, "Arrêtez le bot avant de vider l'historique.")
    await db.clear_history()
    bus.log("Historique vidé.", "warning")
    bus.notify("history")
    bus.notify("status")
    return {"ok": True}


@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    if access.enabled() and not access.is_authed(ws.cookies.get(access.COOKIE)):
        await ws.close(code=1008)
        return
    await ws.accept()
    queue = bus.subscribe()
    try:
        for entry in list(bus.history):
            await ws.send_json(entry)
        while True:
            event = await queue.get()
            await ws.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.unsubscribe(queue)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=access.HOST, port=8000)
