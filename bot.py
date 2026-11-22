"""Boucle principale du bot de participation aux concours Instagram."""
from __future__ import annotations

import asyncio
import random
import time
from datetime import date, datetime, timedelta
from typing import Any

import db
import instagram as ig
import keepawake
import priority
from analyzer import AnalyzerError, analyze_caption
from auth import auth
from logbus import bus

ACTION_DELAY = (3.0, 9.0)        # entre deux actions
GIVEAWAY_DELAY = (45.0, 120.0)   # entre deux concours
CYCLE_PAUSE = (900.0, 1800.0)    # entre deux scans quand il n'y avait rien de nouveau
BUSY_CYCLE_PAUSE = (180.0, 480.0)  # entre deux scans quand le dernier en a trouvé du nouveau
FAST_ACTION = (1.5, 4.0)          # préréglage « rapide »
FAST_GIVEAWAY = (10.0, 25.0)
GENERIC_COMMENT = "Je participe ! 🍀"  # si le concours exige un commentaire sans autre précision
BLOCK_COOLDOWN_MIN = 120          # pause après un « Action bloquée », puis reprise automatique
MAX_BLOCKS_24H = 3                # au-delà, le bot s'arrête pour de bon
MAX_FAILURES = 8                  # cycles consécutifs en erreur avant arrêt
MAX_POST_ERRORS = 6               # posts consécutifs en erreur avant redémarrage du navigateur
TRIAGE_DELAY = (2.0, 5.0)         # entre deux lectures de posts (aucune action : plus court)
MAX_TRIAGE_SECONDS = 25 * 60      # durée max de la phase de lecture avant de passer à la participation


class StopBot(Exception):
    pass


class FatalStop(Exception):
    """Arrêt définitif décidé par le bot lui-même (trop de blocages, trop d'échecs)."""


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


class Bot:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.stop_event = asyncio.Event()
        self.activity: str = "À l'arrêt"
        self.last_error: str | None = None
        self.cycles = 0
        self.blocks: list[float] = []

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def snapshot(self) -> dict[str, Any]:
        return {"running": self.running, "activity": self.activity, "last_error": self.last_error, "cycles": self.cycles}

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

    async def _sleep_for(self, seconds: float, label: str | None = None) -> None:
        if label:
            bus.log(f"{label} : pause de {seconds:.0f}s", "debug")
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=max(0.0, seconds))
        except asyncio.TimeoutError:
            return
        raise StopBot()

    async def _sleep(self, a: float, b: float, label: str | None = None) -> None:
        await self._sleep_for(random.uniform(a, b), label)

    @staticmethod
    def _pace(cfg: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]]:
        """(délai entre actions, délai entre concours) selon le préréglage de vitesse."""
        if cfg.get("speed") == "fast":
            return FAST_ACTION, FAST_GIVEAWAY
        return ACTION_DELAY, GIVEAWAY_DELAY

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise StopBot()

    # -- boucle autonome ------------------------------------------------------
    async def _run(self) -> None:
        cfg = await db.get_config()
        mode = "DRY-RUN (simulation)" if cfg["dry_run"] else "RÉEL"
        quota = int(cfg.get("daily_quota") or 0)
        bus.log(f"Démarrage du bot — mode {mode} — mode autonome", "success")
        bus.log(
            f"Options : plafond = {quota if quota > 0 else 'AUCUN'}"
            f" · participer quand même = {'OUI' if cfg.get('force_participate', True) else 'NON'}"
            f" · story/republication = {'OUI' if cfg.get('do_story', True) else 'NON'}"
            f" · analyse = {cfg.get('analyzer_mode', 'rules')} · vitesse = {cfg.get('speed', 'human')}",
            "info",
        )
        if keepawake.enable():
            bus.log("Mise en veille du PC empêchée tant que le bot tourne.", "info")
        failures = 0
        try:
            while True:
                self._check_stop()
                cfg = await db.get_config()
                try:
                    handled = await self._browser_cycle(cfg)
                    failures = 0
                except (StopBot, asyncio.CancelledError, ig.SessionExpired):
                    raise
                except ig.InstagramBlocked as e:
                    await self._on_block(e, cfg)
                    continue
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    self.last_error = f"{type(e).__name__}: {e}"
                    bus.log(
                        f"Erreur : {self.last_error} (échec {failures}/{MAX_FAILURES}) — "
                        "le navigateur est relancé et le bot continue.", "error",
                    )
                    if failures >= MAX_FAILURES:
                        raise FatalStop(f"{MAX_FAILURES} échecs consécutifs : {self.last_error}") from e
                    self._set_activity("Erreur — nouvel essai bientôt")
                    await self._sleep_for(min(60 * failures, 600))
                    continue
                self.cycles += 1
                seconds = random.uniform(*(BUSY_CYCLE_PAUSE if handled else CYCLE_PAUSE))
                at = _clock(datetime.now().timestamp() + seconds)
                self._set_activity(f"Pause — prochain scan à {at}")
                bus.log(
                    f"Cycle {self.cycles} terminé ({handled} post(s) examiné(s)). Prochain scan vers {at}.", "info",
                )
                await self._sleep_for(seconds)
        except (StopBot, asyncio.CancelledError):
            bus.log("Bot arrêté.", "warning")
        except ig.SessionExpired as e:
            self.last_error = str(e)
            auth._set_connected(False)
            bus.log(f"{e} Reconnectez-vous puis relancez le bot. Arrêt.", "error")
        except FatalStop as e:
            self.last_error = str(e)
            bus.log(f"⛔ {e} Arrêt du bot.", "error")
        finally:
            keepawake.disable()
            await ig.browser.release()
            self._set_activity("À l'arrêt")
            if not self.stop_event.is_set():       # arrêt décidé par le bot : on ne le relancera pas tout seul
                await db.set_config({"bot_enabled": False})
            bus.notify("history")
            bus.notify("status")

    async def _browser_cycle(self, cfg: dict[str, Any]) -> int:
        """Ouvre un navigateur neuf, fait un passage complet sur les hashtags, puis le ferme."""
        self._set_activity("Ouverture du navigateur")
        ctx = await ig.browser.acquire("bot", headless=bool(cfg.get("headless", True)))
        try:
            if not await ig.has_session_cookie(ctx):
                auth._set_connected(False)
                raise ig.SessionExpired("Aucune session Instagram : connectez-vous d'abord.")
            auth._set_connected(True)
            page = await ig.get_page(ctx)
            return await self._cycle(page)
        finally:
            await ig.browser.release()

    async def _on_block(self, err: Exception, cfg: dict[str, Any]) -> None:
        now = time.time()
        self.blocks = [t for t in self.blocks if now - t < 86400] + [now]
        self.last_error = str(err)
        if len(self.blocks) >= MAX_BLOCKS_24H:
            raise FatalStop(f"{len(self.blocks)} blocages Instagram en 24 h : arrêt pour ne pas aggraver la restriction.")
        minutes = max(10, int(cfg.get("block_cooldown_min") or BLOCK_COOLDOWN_MIN))
        if any(m in str(err) for m in ig.FREQUENCY_LIMIT_MARKERS):
            # « Nous limitons la fréquence de certaines actions (comme suivre des comptes) » : ce blocage dure en général
            # bien plus de 2 h ; reprendre trop tôt le prolonge. Pause longue.
            minutes = max(minutes, int(cfg.get("follow_block_cooldown_h") or 12) * 60)
        bus.log(
            f"⛔ ACTION BLOQUÉE ({err}) — blocage {len(self.blocks)}/{MAX_BLOCKS_24H} en 24 h. "
            f"Pause de {minutes} min puis reprise automatique vers {_clock(now + minutes * 60)}.",
            "error",
        )
        self._set_activity(f"Blocage Instagram — reprise vers {_clock(now + minutes * 60)}")
        await self._sleep_for(minutes * 60)

    async def _follow_budget(self, cfg: dict[str, Any]) -> None:
        """Attend si le budget d'abonnements (par heure / par jour) est épuisé : mieux vaut attendre que se faire bloquer."""
        for seconds, key, label in ((3600, "max_follows_per_hour", "heure"), (86400, "max_follows_per_day", "jour")):
            limit = int(cfg.get(key) or 0)
            if limit <= 0:
                continue
            done = await db.follow_window(seconds)
            if len(done) >= limit:
                wait = done[len(done) - limit] + seconds - time.time() + random.uniform(20, 90)
                bus.log(
                    f"Budget d'abonnements atteint ({limit}/{label}) : pause de {max(1, int(wait // 60))} min pour ne pas "
                    "déclencher la limite d'Instagram.", "warning",
                )
                self._set_activity(f"Pause abonnements — reprise vers {_clock(time.time() + wait)}")
                await self._sleep_for(max(5.0, wait))

    async def _quota_reached(self, cfg: dict[str, Any]) -> bool:
        quota = int(cfg.get("daily_quota") or 0)
        if quota <= 0:                       # 0 = aucun plafond
            return False
        status = ("Simulé",) if cfg["dry_run"] else ("Participé", "Partiel")
        return await db.count_today(status) >= quota

    async def _respect_quota(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """Plafond optionnel : s'il est atteint, on attend demain au lieu de s'arrêter."""
        while await self._quota_reached(cfg):
            now = datetime.now()
            resume = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
            bus.log(f"Plafond journalier atteint ({cfg['daily_quota']}) : pause jusqu'à {resume.strftime('%H:%M')} demain.", "success")
            self._set_activity(f"Plafond atteint — reprise à {resume.strftime('%H:%M')}")
            await self._sleep_for((resume - now).total_seconds())
            cfg = await db.get_config()
        return cfg

    async def _cycle(self, page) -> int:
        """Un passage : 1) scanner et LIRE les posts, 2) participer du plus prioritaire au moins prioritaire."""
        cfg = await db.get_config()
        hashtags = [h.strip().lstrip("#") for h in cfg["hashtags"] if h.strip()]
        if not hashtags:
            bus.log("Aucun hashtag configuré.", "warning")
            return 0
        await db.purge_candidates(3)
        examined = 0
        post_errors = 0
        started = time.monotonic()
        out_of_time = False

        # ---- phase 1 : scan + lecture (aucune action sur Instagram) ----
        for tag in hashtags:
            if out_of_time:
                break
            self._check_stop()
            cfg = await db.get_config()  # la config est relue en direct
            self._set_activity(f"Scan #{tag}")
            bus.log(f"🔎 Scan du hashtag #{tag}…")
            limit = int(cfg.get("posts_per_hashtag", 12))
            urls = await ig.collect_hashtag_posts(page, tag, limit * 2)
            new_urls = [u for u in urls if not await db.url_known(u)][:limit]
            bus.log(f"#{tag} : {len(urls)} posts trouvés, {len(new_urls)} nouveaux.")
            for url in new_urls:
                self._check_stop()
                if time.monotonic() - started > MAX_TRIAGE_SECONDS:
                    bus.log(f"Lecture limitée à {MAX_TRIAGE_SECONDS // 60} min : on passe à la participation.", "warning")
                    out_of_time = True
                    break
                examined += 1
                try:
                    await self._triage(page, url, tag, cfg)
                    post_errors = 0
                except (ig.InstagramBlocked, ig.SessionExpired, StopBot, asyncio.CancelledError):
                    raise
                except Exception as e:  # noqa: BLE001  — un post en erreur ne doit jamais arrêter le bot
                    post_errors += 1
                    await db.add_history(url, None, tag, "Erreur", f"{type(e).__name__}: {str(e)[:150]}")
                    bus.log(f"Erreur sur {url} : {type(e).__name__}: {e} — on passe au suivant.", "error")
                    bus.notify("history")
                    if post_errors >= MAX_POST_ERRORS:
                        raise RuntimeError(f"{post_errors} posts consécutifs en erreur") from e
                await self._sleep(*TRIAGE_DELAY)

        # ---- phase 2 : participation, du plus prioritaire au moins prioritaire ----
        await self._log_queue(await db.get_config())
        post_errors = 0
        while True:
            self._check_stop()
            cfg = await self._respect_quota(await db.get_config())
            cand = await self._next_candidate(cfg)
            if cand is None:
                break
            bus.log(f"🎯 Prochain concours : @{cand['author'] or '?'} — score {cand['score']} ({cand['why']})", "success")
            participated = False
            try:
                participated = await self._participate(page, cand, cfg)
                post_errors = 0
            except (ig.InstagramBlocked, ig.SessionExpired, StopBot, asyncio.CancelledError):
                raise  # le concours reste en file d'attente et sera repris
            except Exception as e:  # noqa: BLE001
                post_errors += 1
                await db.add_history(cand["url"], cand["author"], cand["hashtag"], "Erreur",
                                     f"{type(e).__name__}: {str(e)[:150]}", caption=cand["caption"])
                bus.log(f"Erreur sur {cand['url']} : {type(e).__name__}: {e} — on passe au suivant.", "error")
                bus.notify("history")
                if post_errors >= MAX_POST_ERRORS:
                    await db.remove_candidate(cand["url"])
                    raise RuntimeError(f"{post_errors} participations consécutives en erreur") from e
            await db.remove_candidate(cand["url"])
            await self._sleep(*(self._pace(cfg)[1] if participated else self._pace(cfg)[0]),
                              label="Délai anti-détection entre concours" if participated else None)
        return examined

    async def _ranked_candidates(self, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        """File d'attente triée. Écarte (et note dans l'historique) les concours expirés pendant l'attente."""
        today = date.today()
        alive: list[dict[str, Any]] = []
        for r in await db.list_candidates():
            d = priority.days_left(r.get("end_date"), today)
            if d is not None and d < 0:
                await db.add_history(r["url"], r["author"], r["hashtag"], "Ignoré",
                                     f"Concours expiré pendant l'attente (fin le {r['end_date']}).", caption=r["caption"])
                await db.remove_candidate(r["url"])
                bus.notify("history")
                continue
            score, why = priority.score_result(r["result"], r["caption"], cfg, today)
            alive.append({**r, "score": score, "why": why, "days": d})
        if cfg.get("priority_mode", "balanced") == "off":
            alive.sort(key=lambda c: c["created_at"])
        else:
            alive.sort(key=lambda c: (-c["score"], c["days"] if c["days"] is not None else 99999, c["created_at"]))
        return alive

    async def _next_candidate(self, cfg: dict[str, Any]) -> dict[str, Any] | None:
        ranked = await self._ranked_candidates(cfg)
        return ranked[0] if ranked else None

    async def _log_queue(self, cfg: dict[str, Any]) -> None:
        ranked = await self._ranked_candidates(cfg)
        if not ranked:
            bus.log("📋 File d'attente vide : rien à faire pour l'instant.", "info")
            return
        mode = priority.MODE_LABELS.get(cfg.get("priority_mode", "balanced"), "?")
        bus.log(f"📋 {len(ranked)} concours en file d'attente (classement : {mode}). Ordre de participation :", "success")
        for n, c in enumerate(ranked[:8], 1):
            bus.log(f"   {n}. @{c['author'] or '?'} — score {c['score']} — {c['why']}", "info")
        if len(ranked) > 8:
            bus.log(f"   … et {len(ranked) - 8} autres", "debug")

    async def _triage(self, page, url: str, tag: str, cfg: dict[str, Any]) -> bool:
        """Lit et analyse un post ; s'il est intéressant il entre dans la file d'attente. Aucune action Instagram."""
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
        result = await analyze_caption(
            caption, author, post["post_date"], image_b64, cfg, post.get("collaborators"), post.get("tagged"),
        )
        bus.log(f"Analyse → giveaway={result['is_giveaway']} actif={result['is_active']} — {result['reason']}", "ai")
        for cond in result.get("conditions", []):
            bus.log(f"   • {cond}", "ai")

        if not (result["is_giveaway"] and result["is_active"]):
            await db.add_history(url, author, tag, "Ignoré", result["reason"], caption=caption)
            bus.log(f"Décision : ignoré ({url})", "info")
            bus.notify("history")
            return False

        score, why = priority.score_result(result, caption, cfg)
        public = {k: v for k, v in result.items() if not k.startswith("_")}
        await db.add_candidate(url, author, tag, caption, public, result.get("end_date"))
        bus.log(f"⭐ Ajouté à la file d'attente — score {score} ({why})", "success")
        return True

    async def _participate(self, page, cand: dict[str, Any], cfg: dict[str, Any]) -> bool:
        """Participe à un concours de la file d'attente (like, enregistrer, commentaire, story, follows)."""
        url, author, tag, caption, result = cand["url"], cand["author"], cand["hashtag"], cand["caption"], cand["result"]
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
        await ig.open_post(page, url)
        report: list[dict[str, Any]] = []
        pace = self._pace(cfg)[0]

        async def act(label: str, coro_factory) -> str | None:
            detail = None
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
            return str(detail) if detail is not None else None

        async def on_post():
            if page.url.rstrip("/") != url.rstrip("/"):
                await ig.open_post(page, url)

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
                await self._follow_budget(cfg)
                res = await act(f"suivre @{acc}", lambda acc=acc: ig.follow_account(page, acc))
                if res and "déjà" not in res:
                    await db.log_follow()
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
            skip = txt + ". (Activez « Participer quand même » pour participer malgré cela.)"

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

    needed = max(0, min(needed, len(pool)))
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
