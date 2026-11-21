"""Serveur FastAPI : API REST, logs temps réel (WebSocket) et frontend statique."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db
import instagram as ig
from auth import auth
from bot import bot
from logbus import bus

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    bus.log("Serveur démarré.", "success")
    asyncio.create_task(auth.refresh())
    yield
    if bot.running:
        await bot.stop()
    await ig.browser.release()


app = FastAPI(title="Insta Giveaway Bot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Modèles
# ---------------------------------------------------------------------------
class ConfigIn(BaseModel):
    friends_to_tag: list[str] | None = None
    hashtags: list[str] | None = None
    daily_quota: int | None = Field(default=None, ge=1, le=200)
    dry_run: bool | None = None
    headless: bool | None = None
    posts_per_hashtag: int | None = Field(default=None, ge=1, le=50)


class LoginIn(BaseModel):
    username: str = ""
    password: str = ""
    remember: bool = False


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
    mode_status = "Simulé" if cfg["dry_run"] else "Participé"
    return {
        "bot": bot.snapshot(),
        "session": auth.snapshot(),
        "today": await db.count_today(mode_status),
        "today_real": await db.count_today("Participé"),
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
    if "hashtags" in values:
        values["hashtags"] = _clean_handles(values["hashtags"], "#")
    cfg = await db.set_config(values)
    bus.notify("status")
    return _public_config(cfg)


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
    return res


@app.post("/api/bot/stop")
async def bot_stop():
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

    uvicorn.run("main:app", host="127.0.0.1", port=8000)
