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
FAST_ACTION = (1.5, 4.0)          # préréglage « rapide »
FAST_GIVEAWAY = (10.0, 25.0)
GENERIC_COMMENT = "Je participe ! 🍀"  # si le concours exige un commentaire sans autre précision


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

    @staticmethod
    def _pace(cfg: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]]:
        """(délai entre actions, délai entre concours) selon le préréglage de vitesse."""
        if cfg.get("speed") == "fast":
            return FAST_ACTION, FAST_GIVEAWAY
        return ACTION_DELAY, GIVEAWAY_DELAY

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
            bus.log(f"Erreur d'analyse : {e}. Arrêt du bot.", "error")
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {e}"
            bus.log(f"Erreur inattendue : {self.last_error}. Arrêt du bot.", "error")
        finally:
            await ig.browser.release()
            self._set_activity("À l'arrêt")
            bus.notify("history")

    async def _quota_reached(self, cfg: dict[str, Any]) -> bool:
        status = ("Simulé",) if cfg["dry_run"] else ("Participé", "Partiel")
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
                    await self._sleep(*self._pace(cfg)[1], label="Délai anti-détection entre concours")
                else:
                    await self._sleep(*self._pace(cfg)[0])
            await self._sleep(*self._pace(cfg)[0])

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
        caption = post["caption"]
        if not caption:
            await db.add_history(url, author, tag, "Ignoré", "Légende vide ou introuvable.")
            bus.log(f"{url} : légende introuvable, ignoré.", "warning")
            bus.notify("history")
            return False

        self._set_activity(f"Analyse @{author or '?'}")
        bus.log(f"🔍 Lecture des conditions du post de @{author or '?'} ({url})…", "ai")
        if post.get("collaborators"):
            bus.log("   co-auteurs : " + ", ".join("@" + c for c in post["collaborators"]), "debug")
        if post.get("tagged"):
            bus.log("   comptes identifiés sur la photo : " + ", ".join("@" + t for t in post["tagged"]), "debug")
        image_b64 = None
        if cfg.get("analyzer_mode") in ("ollama", "api"):
            image_b64 = await ig.fetch_image_b64(page, post.get("image_url"))
            bus.log("Image du post envoyée au modèle." if image_b64 else "Image indisponible : légende seule.", "debug")
        try:
            result = await analyze_caption(
                caption, author, post["post_date"], image_b64, cfg,
                post.get("collaborators"), post.get("tagged"),
            )
        except AnalyzerError as e:
            await db.add_history(url, author, tag, "Erreur", str(e), caption=caption)
            bus.notify("history")
            raise
        bus.log(f"Analyse → giveaway={result['is_giveaway']} actif={result['is_active']} — {result['reason']}", "ai")
        for cond in result.get("conditions", []):
            bus.log(f"   • {cond}", "ai")

        if not (result["is_giveaway"] and result["is_active"]):
            await db.add_history(url, author, tag, "Ignoré", result["reason"], caption=caption)
            bus.log(f"Décision : ignoré ({url})", "info")
            bus.notify("history")
            return False

        plan = build_plan(result, cfg)
        for note in plan["notes"]:
            bus.log(f"⚠ {note}", "warning")
        if plan["skip_reason"]:
            await db.add_history(url, author, tag, "Ignoré", plan["skip_reason"], caption=caption)
            bus.log(f"Décision : ignoré — {plan['skip_reason']}", "warning")
            bus.notify("history")
            return False

        reason = result["reason"] + ("" if not plan["notes"] else " ⚠ " + " ; ".join(plan["notes"]))
        comment = plan["comment"]

        if cfg["dry_run"]:
            report = [{"action": a, "ok": None, "detail": "simulé"} for a in plan["steps"]]
            for a in plan["steps"]:
                bus.log(f"🧪 [DRY-RUN] Aurait fait : {a}", "action")
            await db.add_history(url, author, tag, "Simulé", reason, comment, caption=caption, actions=report)
            bus.notify("history")
            bus.notify("status")
            return True

        # ---- mode réel : chaque action est indépendante, un échec n'annule pas les suivantes ----
        self._set_activity(f"Participation @{author or '?'}")
        report: list[dict[str, Any]] = []
        pace = self._pace(cfg)[0]

        async def act(label: str, coro_factory) -> None:
            try:
                detail = await coro_factory()
                report.append({"action": label, "ok": True, "detail": str(detail)})
                bus.log(f"✓ {label} : {detail}", "action")
            except (ig.InstagramBlocked, ig.SessionExpired, StopBot, asyncio.CancelledError):
                report.append({"action": label, "ok": False, "detail": "interrompu"})
                raise
            except Exception as e:  # noqa: BLE001
                report.append({"action": label, "ok": False, "detail": str(e)[:200]})
                bus.log(f"✗ {label} : {e}", "error")
            await self._sleep(*pace)

        async def on_post():
            if page.url.rstrip("/") != url.rstrip("/"):
                await page.goto(url, wait_until="domcontentloaded")
                await ig.human_delay(2, 4)

        async def do_story():
            await on_post()
            r = await ig.share_to_story(page)
            if r.startswith("republié") and ig.last_share_options:
                bus.log("   menu « Partager » proposait : " + " | ".join(ig.last_share_options), "debug")
            return r

        try:
            await act("liker", lambda: ig.like_post(page))
            if plan["save"]:
                await act("enregistrer le post", lambda: ig.save_post(page))
            if comment:
                await act(f"commenter « {comment} »", lambda: ig.post_comment(page, url, comment))
            if plan["story"]:
                await act("republier / mettre en story", do_story)
            for acc in plan["follows"]:
                self._check_stop()
                await act(f"suivre @{acc}", lambda acc=acc: ig.follow_account(page, acc))
        except (ig.InstagramBlocked, ig.SessionExpired, StopBot) as e:
            msg = "Participation interrompue (arrêt)." if isinstance(e, StopBot) else "Action bloquée par Instagram."
            await db.add_history(url, author, tag, "Erreur", msg, comment, caption=caption, actions=report)
            bus.notify("history")
            raise

        ok = sum(1 for r in report if r["ok"])
        failed = [r["action"] for r in report if not r["ok"]]
        status = "Participé" if not failed else ("Partiel" if ok else "Erreur")
        if failed:
            reason += f" ✗ Échec : {', '.join(failed)}."
        await db.add_history(url, author, tag, status, reason, comment, caption=caption, actions=report)
        bus.log(
            f"{'✅' if status == 'Participé' else '⚠'} {status} ({ok}/{len(report)} actions réussies) : {url}",
            "success" if status == "Participé" else "warning",
        )
        bus.notify("history")
        bus.notify("status")
        return status != "Erreur"


def build_plan(result: dict[str, Any], cfg: dict[str, Any], rng: random.Random | None = None) -> dict[str, Any]:
    """Transforme l'analyse en liste exacte d'actions (utilisé par le bot et par « Tester une légende »)."""
    rng = rng or random.Random()
    force = bool(cfg.get("force_participate", True))
    notes: list[str] = []
    skip: str | None = None

    if result.get("unsupported"):
        txt = "Condition(s) impossible(s) pour le bot : " + " ; ".join(result["unsupported"])
        if force:
            notes.append(txt + " → participation partielle")
        else:
            skip = txt + "."

    pool = [f.strip().lstrip("@") for f in cfg.get("friends_to_tag", []) if f.strip()]
    needed = int(result.get("friends_count") or 0)
    if needed > len(pool):
        if force:
            notes.append(f"{needed} ami(s) requis mais {len(pool)} configuré(s) : tous ceux disponibles sont identifiés")
            needed = len(pool)
        else:
            skip = skip or f"{needed} ami(s) à identifier requis mais seulement {len(pool)} configuré(s)."

    answer = result.get("answer")
    if result.get("question") and not answer:
        answer = (cfg.get("default_answer") or "").strip() or None
        if answer:
            notes.append(f"réponse par défaut utilisée pour : « {result['question']} »")
        elif not force:
            skip = skip or "Une question demande une réponse et aucune réponse par défaut n'est configurée."

    friends = rng.sample(pool, needed) if needed else []
    parts = [p for p in (result.get("custom_keyword"), answer) if p] + [f"@{f}" for f in friends]
    if not parts and result.get("needs_comment"):
        parts = [GENERIC_COMMENT]
    comment = " ".join(parts) if parts else None
    story = bool(result.get("needs_story")) and bool(cfg.get("do_story", True))
    follows = list(result.get("accounts_to_follow") or [])
    save = bool(result.get("needs_save"))

    steps = ["liker"]
    if save:
        steps.append("enregistrer le post")
    if comment:
        steps.append(f"commenter « {comment} »")
    if story:
        steps.append("republier / mettre en story")
    steps += [f"suivre @{a}" for a in follows]
    return {
        "follows": follows, "save": save, "story": story, "comment": comment,
        "answer": answer, "friends": friends, "steps": steps, "notes": notes, "skip_reason": skip,
    }

bot = Bot()
