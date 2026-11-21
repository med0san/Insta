"""Gestion du navigateur Playwright (session persistante ./ig_session) et actions Instagram."""
from __future__ import annotations

import asyncio
import random
import re
import shutil
import time
from pathlib import Path
from typing import Any

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    async_playwright,
)

from logbus import bus

BASE_DIR = Path(__file__).resolve().parent
SESSION_DIR = BASE_DIR / "ig_session"
IG = "https://www.instagram.com"

BLOCK_PATTERNS = [
    "action blocked",
    "action bloquée",
    "try again later",
    "réessayez plus tard",
    "we restrict certain activity",
    "nous limitons certaines activités",
    "your account has been temporarily",
    "votre compte a été temporairement",
]


class BrowserBusy(RuntimeError):
    pass


class InstagramBlocked(RuntimeError):
    pass


class SessionExpired(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Délais "humains"
# ---------------------------------------------------------------------------
async def human_delay(a: float = 3.0, b: float = 9.0) -> None:
    await asyncio.sleep(random.uniform(a, b))


async def human_type(page: Page, text: str) -> None:
    """Tape caractère par caractère avec une cadence irrégulière."""
    for ch in text:
        await page.keyboard.type(ch)
        await asyncio.sleep(random.uniform(0.05, 0.22))
        if ch == " " and random.random() < 0.15:
            await asyncio.sleep(random.uniform(0.3, 0.9))


# ---------------------------------------------------------------------------
# Gestionnaire de navigateur : un seul contexte persistant à la fois
# (Chromium verrouille le dossier user_data_dir).
# ---------------------------------------------------------------------------
class BrowserManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.owner: str | None = None
        self._pw: Playwright | None = None
        self.context: BrowserContext | None = None
        self._chrome_version: str | None = None

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def _user_agent(self, pw: Playwright) -> str:
        if not self._chrome_version:
            browser = await pw.chromium.launch(headless=True)
            self._chrome_version = browser.version
            await browser.close()
        major = self._chrome_version.split(".")[0]
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        )

    async def acquire(self, owner: str, headless: bool) -> BrowserContext:
        if self._lock.locked():
            raise BrowserBusy(f"Le navigateur est déjà utilisé ({self.owner}).")
        await self._lock.acquire()
        self.owner = owner
        try:
            SESSION_DIR.mkdir(exist_ok=True)
            self._pw = await async_playwright().start()
            kwargs: dict[str, Any] = dict(
                user_data_dir=str(SESSION_DIR),
                headless=headless,
                viewport={"width": 1280, "height": 900},
                locale="fr-FR",
                args=["--disable-blink-features=AutomationControlled"],
            )
            if headless:
                # Le user-agent headless contient "HeadlessChrome" : on le masque.
                kwargs["user_agent"] = await self._user_agent(self._pw)
            self.context = await self._pw.chromium.launch_persistent_context(**kwargs)
            await self.context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            return self.context
        except Exception:
            await self.release()
            raise

    async def release(self) -> None:
        try:
            if self.context is not None:
                try:
                    await self.context.close()
                except Exception:
                    pass
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
        finally:
            self.context = None
            self._pw = None
            self.owner = None
            if self._lock.locked():
                self._lock.release()


browser = BrowserManager()


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
async def has_session_cookie(ctx: BrowserContext) -> bool:
    now = time.time()
    for c in await ctx.cookies(IG):
        if c.get("name") == "sessionid" and c.get("value"):
            exp = c.get("expires", -1)
            if exp in (-1, None) or exp > now:
                return True
    return False


async def check_session() -> bool:
    ctx = await browser.acquire("vérification de session", headless=True)
    try:
        return await has_session_cookie(ctx)
    finally:
        await browser.release()


def clear_session() -> None:
    if SESSION_DIR.exists():
        shutil.rmtree(SESSION_DIR, ignore_errors=True)


async def get_page(ctx: BrowserContext) -> Page:
    return ctx.pages[0] if ctx.pages else await ctx.new_page()


async def dismiss_cookie_banner(page: Page) -> None:
    pattern = re.compile(
        r"(Allow all cookies|Autoriser tous les cookies|Decline optional cookies|"
        r"Refuser les cookies optionnels|Tout accepter|Accept all)",
        re.I,
    )
    try:
        btn = page.get_by_role("button", name=pattern).first
        if await btn.is_visible(timeout=2500):
            await btn.click()
            await asyncio.sleep(1)
    except Exception:
        pass


async def dismiss_popups(page: Page) -> None:
    """Ferme les popups 'Enregistrer vos identifiants ?' / 'Activer les notifications'."""
    pattern = re.compile(r"^(Not now|Plus tard|Pas maintenant)$", re.I)
    for _ in range(2):
        try:
            btn = page.get_by_role("button", name=pattern).first
            if await btn.is_visible(timeout=1500):
                await btn.click()
                await asyncio.sleep(1)
        except Exception:
            break


# ---------------------------------------------------------------------------
# Connexion par identifiants (avec 2FA éventuelle)
# ---------------------------------------------------------------------------
TWOFA_SELECTOR = 'input[name="verificationCode"], input[name="security_code"]'


async def _wait_login_outcome(page: Page, ctx: BrowserContext, timeout: float = 30) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await has_session_cookie(ctx):
            return {"status": "ok"}
        url = page.url
        if "two_factor" in url:
            return {"status": "2fa_required"}
        try:
            if await page.locator(TWOFA_SELECTOR).first.is_visible(timeout=200):
                return {"status": "2fa_required"}
        except Exception:
            pass
        if "/challenge/" in url or "/auth_platform/" in url:
            return {
                "status": "challenge",
                "message": "Instagram demande une vérification de sécurité. "
                "Utilisez la connexion manuelle (Chromium).",
            }
        try:
            alert = page.locator('#slfErrorAlert, [role="alert"]').first
            if await alert.is_visible(timeout=200):
                text = (await alert.inner_text()).strip()
                if text:
                    return {"status": "error", "message": text}
        except Exception:
            pass
        await asyncio.sleep(1)
    return {"status": "error", "message": "Délai dépassé sans confirmation de connexion."}


async def login_with_credentials(ctx: BrowserContext, username: str, password: str) -> dict[str, str]:
    page = await get_page(ctx)
    await page.goto(f"{IG}/accounts/login/", wait_until="domcontentloaded")
    await dismiss_cookie_banner(page)
    if await has_session_cookie(ctx):
        return {"status": "ok"}

    user_input = page.locator('input[name="username"], input[name="email"]').first
    pass_input = page.locator('input[name="password"], input[name="pass"]').first
    await user_input.wait_for(state="visible", timeout=20000)
    await user_input.click()
    await human_type(page, username)
    await human_delay(0.6, 1.5)
    await pass_input.click()
    await human_type(page, password)
    await human_delay(0.5, 1.2)
    await page.keyboard.press("Enter")

    outcome = await _wait_login_outcome(page, ctx)
    if outcome["status"] == "ok":
        await dismiss_popups(page)
    return outcome


async def submit_2fa(ctx: BrowserContext, code: str) -> dict[str, str]:
    page = await get_page(ctx)
    field = page.locator(TWOFA_SELECTOR).first
    await field.wait_for(state="visible", timeout=10000)
    await field.click()
    await field.fill("")
    await human_type(page, code.strip())
    await human_delay(0.5, 1.2)
    await page.keyboard.press("Enter")
    outcome = await _wait_login_outcome(page, ctx)
    if outcome["status"] == "2fa_required":
        outcome = {"status": "2fa_required", "message": "Code refusé, réessayez."}
    if outcome["status"] == "ok":
        await dismiss_popups(page)
    return outcome


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------
POST_RE = re.compile(r"^/(?:[A-Za-z0-9._]+/)?(p|reel)/([A-Za-z0-9_-]+)/?")


def normalize_post_url(href: str) -> str | None:
    href = href.replace(IG, "")
    m = POST_RE.match(href)
    if not m:
        return None
    return f"{IG}/{m.group(1)}/{m.group(2)}/"


async def check_blocked(page: Page) -> None:
    try:
        text = (await page.locator("body").inner_text(timeout=4000)).lower()
    except Exception:
        return
    for pat in BLOCK_PATTERNS:
        if pat in text:
            raise InstagramBlocked(f"Instagram affiche « {pat} »")


def check_logged_in(page: Page) -> None:
    if "/accounts/login" in page.url or "/challenge/" in page.url:
        raise SessionExpired("Session Instagram expirée ou vérification requise.")


async def collect_hashtag_posts(page: Page, tag: str, limit: int) -> list[str]:
    await page.goto(f"{IG}/explore/tags/{tag}/", wait_until="domcontentloaded")
    await human_delay(3, 6)
    check_logged_in(page)
    await check_blocked(page)
    urls: list[str] = []
    for _ in range(4):
        hrefs = await page.eval_on_selector_all(
            'a[href*="/p/"], a[href*="/reel/"]', "els => els.map(e => e.getAttribute('href'))"
        )
        for h in hrefs:
            u = normalize_post_url(h or "")
            if u and u not in urls:
                urls.append(u)
        if len(urls) >= limit:
            break
        await page.mouse.wheel(0, random.randint(900, 1600))
        await human_delay(1.5, 3.5)
    return urls[:limit]


AUTHOR_RE = re.compile(r"-\s*([A-Za-z0-9._]{1,30})\s+(?:on|le|el|am)\s", re.I)


async def read_post(page: Page, url: str) -> dict[str, Any]:
    await page.goto(url, wait_until="domcontentloaded")
    await human_delay(2.5, 5)
    check_logged_in(page)
    await check_blocked(page)

    caption = ""
    try:
        h1 = page.locator("h1").first
        if await h1.is_visible(timeout=4000):
            caption = (await h1.inner_text()).strip()
    except Exception:
        pass

    og_desc = ""
    try:
        og_desc = await page.get_attribute('meta[property="og:description"]', "content", timeout=2000) or ""
    except Exception:
        pass
    if not caption and og_desc:
        m = re.search(r':\s*["“](.*)["”]\s*\.?\s*$', og_desc, re.S)
        caption = (m.group(1) if m else og_desc).strip()

    author = None
    m = AUTHOR_RE.search(og_desc)
    if m:
        author = m.group(1)
    if not author:
        try:
            href = await page.locator("header a[href^='/']").first.get_attribute("href", timeout=2000)
            if href:
                author = href.strip("/").split("/")[0] or None
        except Exception:
            pass

    post_date = None
    try:
        post_date = await page.locator("time[datetime]").first.get_attribute("datetime", timeout=2000)
    except Exception:
        pass

    return {"caption": caption, "author": author, "post_date": post_date}


# ---------------------------------------------------------------------------
# Actions (mode réel)
# ---------------------------------------------------------------------------
LIKE_SEL = 'svg[aria-label="Like"], svg[aria-label="J’aime"], svg[aria-label="J\'aime"]'
UNLIKE_SEL = (
    'svg[aria-label="Unlike"], svg[aria-label="Je n’aime plus"], svg[aria-label="Je n\'aime plus"]'
)


async def like_post(page: Page) -> str:
    try:
        if await page.locator(UNLIKE_SEL).first.is_visible(timeout=1500):
            return "déjà liké"
    except Exception:
        pass
    like = page.locator(LIKE_SEL).first
    await like.wait_for(state="visible", timeout=8000)
    await like.click()
    await asyncio.sleep(2)
    await check_blocked(page)
    return "liké"


async def follow_account(page: Page, account: str) -> str:
    await page.goto(f"{IG}/{account}/", wait_until="domcontentloaded")
    await human_delay(2.5, 5)
    check_logged_in(page)
    await check_blocked(page)
    already = page.get_by_role(
        "button", name=re.compile(r"^(Following|Abonné\(e\)|Abonné|Abonnée|Requested|Demandé)", re.I)
    )
    try:
        if await already.first.is_visible(timeout=1500):
            return "déjà suivi"
    except Exception:
        pass
    btn = page.get_by_role(
        "button", name=re.compile(r"^(Follow|Suivre|Follow Back|Suivre en retour)$", re.I)
    ).first
    try:
        await btn.wait_for(state="visible", timeout=8000)
    except PWTimeout:
        return "bouton Suivre introuvable"
    await btn.click()
    await asyncio.sleep(2)
    await check_blocked(page)
    return "suivi"


async def post_comment(page: Page, url: str, text: str) -> str:
    if page.url.rstrip("/") != url.rstrip("/"):
        await page.goto(url, wait_until="domcontentloaded")
        await human_delay(2.5, 5)
    sel = 'textarea[aria-label*="omment" i], textarea[placeholder*="omment" i], form textarea'
    box = page.locator(sel).first
    await box.wait_for(state="visible", timeout=10000)
    await box.click()
    await asyncio.sleep(random.uniform(0.6, 1.4))
    # le textarea est re-rendu au focus : on retape via le clavier global
    await human_type(page, text + " ")
    await asyncio.sleep(random.uniform(1.0, 2.0))
    # Ferme une éventuelle liste d'autocomplétion des mentions puis publie
    published = False
    try:
        post_btn = page.locator(
            'div[role="button"]:text-is("Post"), div[role="button"]:text-is("Publier"), '
            'button:text-is("Post"), button:text-is("Publier")'
        ).first
        if await post_btn.is_visible(timeout=2500):
            await post_btn.click()
            published = True
    except Exception:
        pass
    if not published:
        await page.keyboard.press("Enter")
    await asyncio.sleep(3)
    await check_blocked(page)
    return "commenté"
