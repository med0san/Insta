"""Orchestration de la connexion Instagram (identifiants + 2FA, ou manuelle)."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

import db
import instagram as ig
from logbus import bus

TWOFA_TIMEOUT = 300      # secondes avant abandon d'une 2FA en attente
MANUAL_TIMEOUT = 600     # secondes max pour une connexion manuelle


class AuthManager:
    def __init__(self) -> None:
        self.connected: bool | None = None      # None = inconnu
        self.checked_at: str | None = None
        self.state: str = "idle"                # idle | logging_in | 2fa_required | manual
        self.message: str | None = None
        self._twofa_task: asyncio.Task | None = None
        self._manual_task: asyncio.Task | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "checked_at": self.checked_at,
            "state": self.state,
            "message": self.message,
        }

    def _set_connected(self, value: bool) -> None:
        self.connected = value
        self.checked_at = datetime.now().strftime("%H:%M:%S")
        bus.notify("status")

    async def refresh(self) -> dict[str, Any]:
        if ig.browser.busy:
            return self.snapshot()
        try:
            self._set_connected(await ig.check_session())
        except Exception as e:
            bus.log(f"Vérification de session impossible : {e}", "error")
        return self.snapshot()

    # -- identifiants -------------------------------------------------------
    async def login(self, username: str, password: str, headless: bool) -> dict[str, Any]:
        if self.state != "idle":
            return {"status": "error", "message": "Une connexion est déjà en cours."}
        ctx = await ig.browser.acquire("connexion Instagram", headless=headless)
        self.state, self.message = "logging_in", None
        bus.notify("status")
        bus.log(f"Connexion à Instagram en tant que @{username}…")
        keep_open = False
        try:
            outcome = await ig.login_with_credentials(ctx, username, password)
            if outcome["status"] == "2fa_required":
                keep_open = True
                self.state = "2fa_required"
                self.message = "Code de vérification (2FA) demandé par Instagram."
                bus.log("Instagram demande un code 2FA : saisissez-le dans l'interface.", "warning")
                self._twofa_task = asyncio.create_task(self._twofa_timeout())
            else:
                self._finish(outcome)
            return outcome
        except Exception as e:
            outcome = {"status": "error", "message": f"Échec de la connexion : {e}"}
            self._finish(outcome)
            return outcome
        finally:
            if not keep_open:
                await ig.browser.release()
                self.state = "idle" if self.state != "2fa_required" else self.state
                bus.notify("status")

    async def submit_2fa(self, code: str) -> dict[str, Any]:
        if self.state != "2fa_required" or ig.browser.context is None:
            return {"status": "error", "message": "Aucune vérification 2FA en attente."}
        bus.log("Envoi du code 2FA…")
        try:
            outcome = await ig.submit_2fa(ig.browser.context, code)
        except Exception as e:
            outcome = {"status": "error", "message": f"Échec 2FA : {e}"}
        if outcome["status"] == "2fa_required":
            self.message = outcome.get("message")
            bus.log(self.message or "Code refusé.", "warning")
            bus.notify("status")
            return outcome
        if self._twofa_task:
            self._twofa_task.cancel()
        await ig.browser.release()
        self.state = "idle"
        self._finish(outcome)
        return outcome

    async def _twofa_timeout(self) -> None:
        try:
            await asyncio.sleep(TWOFA_TIMEOUT)
        except asyncio.CancelledError:
            return
        if self.state == "2fa_required":
            await ig.browser.release()
            self.state, self.message = "idle", "Délai 2FA expiré."
            bus.log("Délai 2FA expiré, connexion abandonnée.", "error")
            bus.notify("status")

    def _finish(self, outcome: dict[str, Any]) -> None:
        if outcome["status"] == "ok":
            self.message = None
            bus.log("Connecté à Instagram, session sauvegardée dans ./ig_session.", "success")
            self._set_connected(True)
        else:
            self.message = outcome.get("message")
            bus.log(self.message or "Connexion échouée.", "error")
            self._set_connected(False)

    # -- manuelle -----------------------------------------------------------
    async def start_manual(self) -> dict[str, Any]:
        if self.state != "idle":
            return {"status": "error", "message": "Une connexion est déjà en cours."}
        ctx = await ig.browser.acquire("connexion manuelle", headless=False)
        self.state = "manual"
        self.message = "Connectez-vous dans la fenêtre Chromium ouverte."
        bus.notify("status")
        self._manual_task = asyncio.create_task(self._manual_flow(ctx))
        return {"status": "manual", "message": self.message}

    async def _manual_flow(self, ctx) -> None:
        closed = asyncio.Event()
        ctx.on("close", lambda *_: closed.set())
        try:
            page = await ig.get_page(ctx)
            page.on("close", lambda *_: closed.set() if not ctx.pages else None)
            await page.goto(f"{ig.IG}/accounts/login/", wait_until="domcontentloaded")
            bus.log("Fenêtre Chromium ouverte : connectez-vous manuellement.", "info")
            deadline = time.monotonic() + MANUAL_TIMEOUT
            while time.monotonic() < deadline and not closed.is_set():
                if await ig.has_session_cookie(ctx):
                    await asyncio.sleep(3)  # laisse Instagram finir d'écrire ses cookies
                    self._finish({"status": "ok"})
                    return
                await asyncio.sleep(2)
            self._finish({"status": "error", "message": "Fenêtre fermée ou délai dépassé sans connexion."})
        except Exception as e:
            if closed.is_set():
                self._finish({"status": "error", "message": "Fenêtre fermée avant la connexion."})
            else:
                self._finish({"status": "error", "message": f"Connexion manuelle échouée : {e}"})
        finally:
            await ig.browser.release()
            self.state = "idle"
            bus.notify("status")

    async def logout(self) -> None:
        if ig.browser.busy:
            raise ig.BrowserBusy(f"Navigateur occupé ({ig.browser.owner}).")
        ig.clear_session()
        await db.set_config({"ig_password": ""})
        self._set_connected(False)
        bus.log("Session Instagram supprimée (./ig_session effacé).", "warning")


auth = AuthManager()
