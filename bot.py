"""Boucle principale du bot de participation aux concours Instagram."""
from __future__ import annotations

import asyncio
import random
from typing import Any

import db
import instagram as ig
from analyzer import AnalyzerError, analyze_caption
from auth import auth
from logbus import bus

ACTION_DELAY = (3.0, 9.0)        # entre deux actions
GIVEAWAY_DELAY = (45.0, 120.0)   # entre deux concours
CYCLE_PAUSE = (900.0, 1800.0)    # après avoir parcouru tous les hashtags


class StopBot(Exception):
    pass


class Bot:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.stop_event = asyncio.Event()
        self.activity: str = "À l'arrêt"
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def snapshot(self) -> dict[str, Any]:
        return {"running": self.running, "activity": self.activity, "last_error": self.last_error}

    def _set_activity(self, text: str) -> None:
        self.activity = text
        bus.notify("status")

    # -- contrôle -----------------------------------------------------------
    async def start(self) -> dict[str, Any]:
        if self.running:
            return {"ok": False, "message": "Le bot tourne déjà."}
        if ig.browser.busy:
            return {"ok": False, "message": f"Navigateur occupé ({ig.browser.owner})."}
        self.stop_event = asyncio.Event()
        self.last_error = None
        self.task = asyncio.create_task(self._run())
        return {"ok": True}

    async def stop(self) -> dict[str, Any]:
        if not self.running:
            return {"ok": False, "message": "Le bot n'est pas lancé."}
        bus.log("Arrêt du bot demandé…", "warning")
        self.stop_event.set()
        self.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(self.task), timeout=10)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
        return {"ok": True}

    async def _sleep(self, a: float, b: float, label: str | None = None) -> None:
        seconds = random.uniform(a, b)
        if label:
            bus.log(f"{label} : pause de {seconds:.0f}s", "debug")
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
        raise StopBot()

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise StopBot()

    # -- boucle -------------------------------------------------------------
    async def _run(self) -> None:
        cfg = await db.get_config()
        mode = "DRY-RUN (simulation)" if cfg["dry_run"] else "RÉEL"
        bus.log(f"Démarrage du bot — mode {mode}", "success")
        self._set_activity("Ouverture du navigateur")
        try:
            ctx = await ig.browser.acquire("bot", headless=bool(cfg.get("headless", True)))
        except Exception as e:
            self.last_error = str(e)
            bus.log(f"Impossible d'ouvrir le navigateur : {e}", "error")
            self._set_activity("À l'arrêt")
            return
        try:
            if not await ig.has_session_cookie(ctx):
                auth._set_connected(False)
                raise ig.SessionExpired("Aucune session Instagram : connectez-vous d'abord.")
            auth._set_connected(True)
            page = await ig.get_page(ctx)
            while True:
                await self._cycle(page)
                self._set_activity("Pause entre deux cycles")
                await self._sleep(*CYCLE_PAUSE, label="Cycle terminé, nouveau scan plus tard")
        except StopBot:
            bus.log("Bot arrêté.", "warning")
        except asyncio.CancelledError:
            bus.log("Bot arrêté.", "warning")
        except ig.InstagramBlocked as e:
            self.last_error = str(e)
            bus.log(f"⛔ ACTION BLOQUÉE — {e}. Arrêt immédiat du bot.", "error")
        except ig.SessionExpired as e:
            self.last_error = str(e)
            auth._set_connected(False)
            bus.log(f"{e} Arrêt du bot.", "error")
        except AnalyzerError as e:
            self.last_error = str(e)
            bus.log(f"Erreur d'analyse IA : {e}. Arrêt du bot.", "error")
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {e}"
            bus.log(f"Erreur inattendue : {self.last_error}. Arrêt du bot.", "error")
        finally:
            await ig.browser.release()
            self._set_activity("À l'arrêt")
            bus.notify("history")

    async def _quota_reached(self, cfg: dict[str, Any]) -> bool:
        status = "Simulé" if cfg["dry_run"] else "Participé"
        done = await db.count_today(status)
        return done >= int(cfg["daily_quota"])

    async def _cycle(self, page) -> None:
        cfg = await db.get_config()
        hashtags = [h.strip().lstrip("#") for h in cfg["hashtags"] if h.strip()]
        if not hashtags:
            bus.log("Aucun hashtag configuré.", "warning")
            return
        for tag in hashtags:
            self._check_stop()
            cfg = await db.get_config()  # la config est relue en direct
            if await self._quota_reached(cfg):
                bus.log(f"Quota journalier atteint ({cfg['daily_quota']}). Arrêt du bot.", "success")
                raise StopBot()
            self._set_activity(f"Scan #{tag}")
            bus.log(f"🔎 Scan du hashtag #{tag}…")
            limit = int(cfg.get("posts_per_hashtag", 12))
            urls = await ig.collect_hashtag_posts(page, tag, limit * 2)
            new_urls = [u for u in urls if not await db.url_exists(u)][:limit]
            bus.log(f"#{tag} : {len(urls)} posts trouvés, {len(new_urls)} nouveaux.")
            for url in new_urls:
                self._check_stop()
                cfg = await db.get_config()
                if await self._quota_reached(cfg):
                    bus.log(f"Quota journalier atteint ({cfg['daily_quota']}). Arrêt du bot.", "success")
                    raise StopBot()
                participated = await self._process_post(page, url, tag, cfg)
                if participated:
                    await self._sleep(*GIVEAWAY_DELAY, label="Délai anti-détection entre concours")
                else:
                    await self._sleep(*ACTION_DELAY)
            await self._sleep(*ACTION_DELAY)

    async def _process_post(self, page, url: str, tag: str, cfg: dict[str, Any]) -> bool:
        self._set_activity(f"Lecture {url}")
        try:
            post = await ig.read_post(page, url)
        except (ig.InstagramBlocked, ig.SessionExpired):
            raise
        except Exception as e:
            await db.add_history(url, None, tag, "Erreur", f"Lecture impossible : {e}")
            bus.log(f"Lecture impossible de {url} : {e}", "error")
            bus.notify("history")
            return False

        author = post["author"]
        if not post["caption"]:
            await db.add_history(url, author, tag, "Ignoré", "Légende vide ou introuvable.")
            bus.log(f"{url} : légende introuvable, ignoré.", "warning")
            bus.notify("history")
            return False

        self._set_activity(f"Analyse IA @{author or '?'}")
        bus.log(f"🤖 Analyse IA du post de @{author or '?'}…", "ai")
        try:
            result = await analyze_caption(post["caption"], author, post["post_date"])
        except AnalyzerError as e:
            await db.add_history(url, author, tag, "Erreur", str(e))
            bus.notify("history")
            raise
        bus.log(
            f"IA → giveaway={result['is_giveaway']} actif={result['is_active']} "
            f"suivre={result['accounts_to_follow']} amis={result['friends_count']} "
            f"mot-clé={result['custom_keyword']!r} — {result['reason']}",
            "ai",
        )

        if not (result["is_giveaway"] and result["is_active"]):
            await db.add_history(url, author, tag, "Ignoré", result["reason"])
            bus.log(f"Décision : ignoré ({url})", "info")
            bus.notify("history")
            return False

        friends_pool = [f.strip().lstrip("@") for f in cfg["friends_to_tag"] if f.strip()]
        needed = result["friends_count"]
        if needed > len(friends_pool):
            reason = (
                f"{needed} ami(s) à identifier requis mais seulement {len(friends_pool)} configuré(s). "
                + result["reason"]
            )
            await db.add_history(url, author, tag, "Ignoré", reason)
            bus.log(f"Décision : ignoré, pas assez d'amis configurés ({needed} requis).", "warning")
            bus.notify("history")
            return False

        tagged = random.sample(friends_pool, needed) if needed else []
        parts = [result["custom_keyword"]] if result["custom_keyword"] else []
        parts += [f"@{f}" for f in tagged]
        comment = " ".join(parts) if parts else None
        follows = result["accounts_to_follow"]

        if cfg["dry_run"]:
            bus.log(f"🧪 [DRY-RUN] Aurait liké : {url}", "action")
            for acc in follows:
                bus.log(f"🧪 [DRY-RUN] Aurait suivi : @{acc}", "action")
            if comment:
                bus.log(f"🧪 [DRY-RUN] Aurait commenté : « {comment} »", "action")
            await db.add_history(url, author, tag, "Simulé", result["reason"], comment)
            bus.notify("history")
            bus.notify("status")
            return True

        # ---- mode réel ----
        self._set_activity(f"Participation @{author or '?'}")
        try:
            r = await ig.like_post(page)
            bus.log(f"❤️ Post {r} : {url}", "action")
            await self._sleep(*ACTION_DELAY)
            for acc in follows:
                self._check_stop()
                r = await ig.follow_account(page, acc)
                bus.log(f"➕ @{acc} : {r}", "action")
                await self._sleep(*ACTION_DELAY)
            if comment:
                r = await ig.post_comment(page, url, comment)
                bus.log(f"💬 Commentaire publié : « {comment} »", "action")
        except (ig.InstagramBlocked, ig.SessionExpired):
            await db.add_history(url, author, tag, "Erreur", "Action bloquée par Instagram.", comment)
            bus.notify("history")
            raise
        except StopBot:
            await db.add_history(url, author, tag, "Erreur", "Participation interrompue (arrêt).", comment)
            bus.notify("history")
            raise
        except Exception as e:
            await db.add_history(url, author, tag, "Erreur", f"Action échouée : {e}", comment)
            bus.log(f"Participation échouée sur {url} : {e}", "error")
            bus.notify("history")
            return False

        await db.add_history(url, author, tag, "Participé", result["reason"], comment)
        bus.log(f"✅ Participation enregistrée pour {url}", "success")
        bus.notify("history")
        bus.notify("status")
        return True


bot = Bot()
