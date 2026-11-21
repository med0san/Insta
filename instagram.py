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
    for _ in range(min(30, max(4, limit // 5))):
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


async def open_post(page: Page, url: str) -> None:
    """Ouvre un post (avant de participer) en vérifiant session et blocage."""
    await page.goto(url, wait_until="domcontentloaded")
    await human_delay(2.5, 5)
    check_logged_in(page)
    await check_blocked(page)


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

    author = await _find_author(page, og_desc)

    post_date = None
    try:
        post_date = await page.locator("time[datetime]").first.get_attribute("datetime", timeout=2000)
    except Exception:
        pass

    image_url = None
    try:
        image_url = await page.get_attribute('meta[property="og:image"]', "content", timeout=1500)
    except Exception:
        pass
    if not image_url:
        try:
            image_url = await page.evaluate(
                """() => {
                    const imgs = [...document.querySelectorAll('article img, main img')]
                      .filter(i => i.naturalWidth >= 400 && /^https?:/.test(i.src));
                    return imgs.length ? imgs[0].src : null;
                }"""
            )
        except Exception:
            pass

    collaborators = [c for c in await _header_accounts(page) if not author or c.lower() != author.lower()]
    tagged = await _tagged_accounts(page)
    return {
        "caption": caption,
        "author": author,
        "post_date": post_date,
        "image_url": image_url,
        "collaborators": collaborators,
        "tagged": tagged,
    }


def _handles_from_hrefs(hrefs: list[str | None]) -> list[str]:
    out: list[str] = []
    for h in hrefs:
        parts = [x for x in (h or "").split("?")[0].strip("/").split("/") if x]
        if len(parts) == 1 and parts[0].lower() not in RESERVED_PATHS and HANDLE_RE.match(parts[0]):
            if parts[0].lower() not in {o.lower() for o in out}:
                out.append(parts[0])
    return out


async def _profile_links(page: Page, selector: str, visible_only: bool = False) -> list[str]:
    js = (
        "els => els.filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')"
        ".map(e => e.getAttribute('href'))"
        if visible_only else "els => els.map(e => e.getAttribute('href'))"
    )
    try:
        hrefs = await page.eval_on_selector_all(selector, js)
    except Exception:
        return []
    return _handles_from_hrefs(hrefs)


async def _header_accounts(page: Page) -> list[str]:
    """Auteur + co-auteurs (post en collaboration « compte1 et compte2 »)."""
    return (await _profile_links(page, "article header a[href^='/'], main header a[href^='/']"))[:4]


TAGS_BTN_SEL = (
    'svg[aria-label="Tags"], svg[aria-label="Tag"], svg[aria-label="Identifications"], '
    'svg[aria-label="Identification"], svg[aria-label="Personnes identifiées"], svg[aria-label="Tagged people"]'
)


async def _tagged_accounts(page: Page) -> list[str]:
    """Comptes identifiés sur la photo : on affiche les identifications et on relève les nouveaux liens de profil."""
    try:
        btn = page.locator(TAGS_BTN_SEL).first
        if not await btn.is_visible(timeout=1200):
            return []
        # liens visibles avant / après l'affichage des identifications (ils peuvent exister cachés dans la page)
        before = set(h.lower() for h in await _profile_links(page, "a[href^='/']", visible_only=True))
        await btn.click()
        await asyncio.sleep(1.2)
        after = await _profile_links(page, "a[href^='/']", visible_only=True)
        return [h for h in after if h.lower() not in before][:15]
    except Exception:
        return []


HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
RESERVED_PATHS = {
    "explore", "reels", "reel", "p", "direct", "accounts", "stories", "about", "legal", "privacy",
    "directory", "web", "api", "challenge", "emails", "session", "tv", "nametag", "download", "void",
}


async def _find_author(page: Page, og_desc: str) -> str | None:
    """Pseudo de l'auteur du post : plusieurs stratégies car le DOM d'Instagram change souvent."""
    m = AUTHOR_RE.search(og_desc)
    if m and HANDLE_RE.match(m.group(1)):
        return m.group(1)
    # <title> / og:title : « pseudo on Instagram: … » (n'est un pseudo valide que sans espace)
    for getter in (page.title, lambda: page.get_attribute('meta[property="og:title"]', "content", timeout=1500)):
        try:
            title = await getter() or ""
        except Exception:
            continue
        t = re.match(r"^\(?\d*\)?\s*(?:.*?\(@([A-Za-z0-9._]{1,30})\)|([A-Za-z0-9._]{1,30})\s+(?:on|sur) Instagram)", title)
        if t:
            return t.group(1) or t.group(2)
    # 1er lien de profil dans la zone du post
    try:
        hrefs = await page.eval_on_selector_all(
            "article a[href^='/'], main a[href^='/'], header a[href^='/']",
            "els => els.map(e => e.getAttribute('href'))",
        )
    except Exception:
        hrefs = []
    for h in hrefs:
        parts = [x for x in (h or "").split("?")[0].strip("/").split("/") if x]
        if len(parts) == 1 and parts[0].lower() not in RESERVED_PATHS and HANDLE_RE.match(parts[0]):
            return parts[0]
    return None


async def fetch_image_b64(page: Page, image_url: str | None) -> str | None:
    """Télécharge l'image du post (pour un modèle vision local). Renvoie du base64 ou None."""
    if not image_url:
        return None
    try:
        resp = await page.context.request.get(image_url, timeout=15000)
        if not resp.ok:
            return None
        body = await resp.body()
        if len(body) > 6_000_000:
            return None
        import base64
        return base64.b64encode(body).decode("ascii")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Actions (mode réel)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Icônes d'action du post (like, enregistrer, partager…)
#
# Une page de post contient AUSSI des cœurs « J'aime » de commentaires, souvent placés AVANT la barre d'actions
# dans le DOM : prendre « la première icône » likait un commentaire (ou rien). On ne retient donc que l'icône qui
# se trouve dans le même bloc que l'icône « Commenter » (= la barre d'actions du post), et on vérifie le résultat.
# ---------------------------------------------------------------------------
LIKE_LABELS = ["Like", "J'aime"]
UNLIKE_LABELS = ["Unlike", "Je n'aime plus"]
SAVE_LABELS = ["Save", "Enregistrer"]
UNSAVE_LABELS = ["Remove", "Retirer", "Supprimer", "Unsave", "Ne plus enregistrer"]
SHARE_LABELS = ["Share", "Share Post", "Partager", "Partager la publication", "Send", "Envoyer"]
REPOST_LABELS = ["Repost", "Republier", "Republication"]
REPOSTED_LABELS = ["Reposted", "Republié", "Republiée", "Remove repost", "Undo repost", "Annuler la republication",
                   "Supprimer la republication"]
COMMENT_LABELS = ["Comment", "Commenter", "Comentar", "Kommentieren"]

_FIND_JS = """
([labels, comments, attr, strict]) => {
  document.querySelectorAll('[' + attr + ']').forEach(e => e.removeAttribute(attr));
  const norm = s => (s || '').replace(/[\\u2019\\u2018]/g, "'").toLowerCase().trim();
  const want = labels.map(norm), com = comments.map(norm);
  const svgs = [...document.querySelectorAll('svg[aria-label]')].filter(e => e.getClientRects().length > 0);
  const commentIcons = svgs.filter(s => com.includes(norm(s.getAttribute('aria-label'))));
  for (const el of svgs) {
    const lab = norm(el.getAttribute('aria-label'));
    if (!want.includes(lab)) continue;
    if (!strict) { el.setAttribute(attr, '1'); return el.getAttribute('aria-label'); }
    // plus petit bloc contenant à la fois cette icône et une icône « Commenter » = la barre d'actions du post.
    // Un cœur de commentaire n'y est jamais seul : son plus petit bloc commun avec « Commenter » contient
    // aussi d'autres cœurs (ceux des autres commentaires et celui du post).
    let p = el.parentElement;
    while (p && !commentIcons.some(c => p.contains(c))) p = p.parentElement;
    if (!p) continue;
    const same = svgs.filter(s => want.includes(norm(s.getAttribute('aria-label'))) && p.contains(s));
    if (same.length === 1) { el.setAttribute(attr, '1'); return el.getAttribute('aria-label'); }
  }
  return null;
}
"""
_ATTR = "data-gb-act"


def _lab(text: str | None) -> str:
    return (text or "").replace("’", "'").replace("‘", "'").lower().strip()


def _is_one_of(label: str | None, labels: list[str]) -> bool:
    return _lab(label) in {_lab(x) for x in labels}


async def _find_action(page: Page, labels: list[str], strict: bool = True) -> str | None:
    """Repère l'icône d'action (et la marque dans le DOM). Renvoie son libellé, ou None."""
    try:
        return await page.evaluate(_FIND_JS, [labels, COMMENT_LABELS, _ATTR, strict])
    except Exception:
        return None


async def _click_action(page: Page) -> None:
    icon = page.locator(f'[{_ATTR}="1"]').first
    button = icon.locator("xpath=ancestor::*[@role='button' or self::button][1]")
    target = button if await button.count() else icon
    await target.scroll_into_view_if_needed(timeout=4000)
    await target.click(timeout=6000)


async def _icons_seen(page: Page) -> str:
    try:
        names = await page.eval_on_selector_all(
            "svg[aria-label]", "els => [...new Set(els.filter(e => e.getClientRects().length).map(e => e.getAttribute('aria-label')))]"
        )
        return ", ".join(names[:25])
    except Exception:
        return "?"


async def _toggle_action(page: Page, off_labels: list[str], on_labels: list[str], what: str, done_word: str) -> str:
    """Clique l'icône du POST puis VÉRIFIE qu'elle est passée à l'état « fait » (plusieurs tentatives)."""
    state = await _find_action(page, off_labels + on_labels)
    if state is None:
        raise RuntimeError(
            f"bouton « {what} » du post introuvable (icônes visibles : {await _icons_seen(page)})"
        )
    if _is_one_of(state, on_labels):
        return f"déjà {done_word}"
    for attempt in range(3):
        if attempt < 2:
            await _click_action(page)
        else:
            await _double_tap_media(page)        # dernier recours pour le like : double-clic sur l'image
        for _ in range(4):                       # l'état met un instant à changer
            await asyncio.sleep(1.0)
            await check_blocked(page)
            state = await _find_action(page, off_labels + on_labels)
            if _is_one_of(state, on_labels):
                return f"{done_word} (vérifié)"
        if state is None:
            break
    raise RuntimeError(f"« {what} » non pris en compte par Instagram (l'icône n'a pas changé d'état)")


async def _double_tap_media(page: Page) -> None:
    media = page.locator("article img, main img").first
    await media.scroll_into_view_if_needed(timeout=3000)
    await media.dblclick(timeout=5000)


async def like_post(page: Page) -> str:
    return await _toggle_action(page, LIKE_LABELS, UNLIKE_LABELS, "J'aime", "liké")


async def save_post(page: Page) -> str:
    # pas de double-clic de secours ici : seul le like l'utilise
    state = await _find_action(page, SAVE_LABELS + UNSAVE_LABELS)
    if state is None:
        raise RuntimeError(f"bouton « Enregistrer » du post introuvable (icônes visibles : {await _icons_seen(page)})")
    if _is_one_of(state, UNSAVE_LABELS):
        return "déjà enregistré"
    for _ in range(2):
        await _click_action(page)
        for _ in range(4):
            await asyncio.sleep(1.0)
            await check_blocked(page)
            if _is_one_of(await _find_action(page, SAVE_LABELS + UNSAVE_LABELS), UNSAVE_LABELS):
                return "enregistré (vérifié)"
    raise RuntimeError("« Enregistrer » non pris en compte par Instagram")


STORY_ITEM_RE = re.compile(r"((?:add|share|post)\b.{0,25}\bstor(?:y|ies)|(?:ajouter|partager|publier)\b.{0,30}\bstory)", re.I)
last_share_options: list[str] = []   # textes vus dans le menu « Partager » (diagnostic affiché dans les logs)


async def share_to_story(page: Page) -> str:
    """Story si le menu de partage la propose, sinon republication (bouton ↻ d'Instagram web)."""
    try:
        if await _find_action(page, SHARE_LABELS) is None:
            await _find_action(page, SHARE_LABELS, strict=False)
        await _click_action(page)
        await asyncio.sleep(2)
        global last_share_options
        try:
            last_share_options = [
                t.strip() for t in await page.locator('[role="dialog"] [role="button"], [role="dialog"] button, [role="dialog"] span')
                .all_inner_texts() if 2 < len(t.strip()) < 40
            ][:20]
        except Exception:
            last_share_options = []
        item = page.get_by_text(STORY_ITEM_RE).first
        if await item.is_visible(timeout=3000):
            await item.click()
            await asyncio.sleep(3)
            confirm = page.get_by_role(
                "button", name=re.compile(r"^(Share|Partager|Share to story|Your story|Votre story|Ta story)", re.I)
            ).first
            await confirm.click(timeout=8000)
            await asyncio.sleep(4)
            await check_blocked(page)
            return "ajouté à votre story"
    except InstagramBlocked:
        raise
    except Exception:
        pass
    try:
        await page.keyboard.press("Escape")  # ferme le menu de partage resté ouvert
    except Exception:
        pass
    await asyncio.sleep(1)

    return await repost_post(page, via_story_note=True)


REPOST_ITEM_RE = re.compile(r"^(Repost|Republier|Republication)$", re.I)
THOUGHT_RE = re.compile(r"(Add a thought|Ajouter une pensée|Ajouter une idée|Partager une pensée|Share a thought)", re.I)
_HTML_JS = """
(attr) => {
  const el = document.querySelector('[' + attr + '="1"]');
  const b = el && (el.closest('[role="button"], button') || el);
  return b ? b.outerHTML.replace(/aria-expanded="[^"]*"/g, '').replace(/\\s+/g, ' ') : null;
}
"""
REPOST_TOAST_RE = re.compile(r"(Reposted|Republié|Republiée|Republication (?:ajoutée|effectuée)|Vous avez republié)", re.I)

_COUNT_JS = """
(attr) => {
  const el = document.querySelector('[' + attr + '="1"]');
  let p = el;
  for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
    const t = (p.innerText || '').trim();
    if (/\\d/.test(t) && t.length < 12) return t;
  }
  return null;
}
"""


def _to_int(text: str | None) -> int | None:
    t = (text or "").strip().replace("\u202f", "").replace(" ", "")
    return int(t) if t.isdigit() else None   # « 1,3 k » : pas comparable à l'unité près


async def _repost_marker(page: Page) -> tuple[str | None, str | None]:
    """(libellé de l'icône republier, compteur affiché à côté)."""
    state = await _find_action(page, REPOST_LABELS + REPOSTED_LABELS)
    if state is None:
        state = await _find_action(page, REPOST_LABELS + REPOSTED_LABELS, strict=False)
    if state is None:
        return None, None
    try:
        return state, await page.evaluate(_COUNT_JS, _ATTR)
    except Exception:
        return state, None


async def repost_post(page: Page, via_story_note: bool = False) -> str:
    """Republie le post (bouton ↻) : ouvre le menu, choisit « Republier », puis VÉRIFIE l'effet."""
    state, before = await _repost_marker(page)
    html_before = await page.evaluate(_HTML_JS, _ATTR) if state else None
    if state is None:
        raise RuntimeError(f"bouton de republication introuvable (icônes visibles : {await _icons_seen(page)})")
    if _is_one_of(state, REPOSTED_LABELS):
        return "déjà republié"
    await _click_action(page)
    await asyncio.sleep(1.5)

    # Instagram propose un petit menu (« Republier » / « Citer ») : on choisit « Republier ».
    # (Le bouton ↻ lui-même est exclu pour ne pas le cliquer une 2e fois, ce qui annulerait la republication.)
    candidates = [
        page.get_by_role("menuitem", name=REPOST_ITEM_RE),
        page.get_by_role("button", name=REPOST_ITEM_RE).filter(has_not=page.locator(f'[{_ATTR}="1"]')),
        page.get_by_text(REPOST_ITEM_RE),
    ]
    menu_seen = False
    new_state, after = state, before
    for cand in candidates:
        try:
            item = cand.first
            if await item.is_visible(timeout=1500):
                await item.click(timeout=4000)
                menu_seen = True
                break
        except Exception:
            continue

    # vérification : l'icône change, le compteur augmente, ou un message de confirmation apparaît
    for _ in range(6):
        await asyncio.sleep(1.0)
        await check_blocked(page)
        new_state, after = await _repost_marker(page)
        if new_state is not None and (_is_one_of(new_state, REPOSTED_LABELS) or _lab(new_state) != _lab(state)):
            return "republié (vérifié)" + (" — la story directe n'est pas proposée sur Instagram web" if via_story_note else "")
        if menu_seen:
            try:
                html_after = await page.evaluate(_HTML_JS, _ATTR)
                if html_after and html_before and html_after != html_before:
                    return "republié (vérifié : l'icône a changé d'état)"
                if await page.get_by_text(THOUGHT_RE).first.is_visible(timeout=300):
                    return "republié (vérifié : votre republication apparaît sur le post)"
            except Exception:
                pass
        b, a = _to_int(before), _to_int(after)
        if b is not None and a is not None and a > b:
            return "republié (vérifié : compteur +1)" + (" — story directe indisponible sur le web" if via_story_note else "")
        try:
            if await page.get_by_text(REPOST_TOAST_RE).first.is_visible(timeout=300):
                return "republié (vérifié : confirmation affichée)"
        except Exception:
            pass
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    raise RuntimeError(
        "republication non confirmée par Instagram"
        + ("" if menu_seen else " (le choix « Republier » n'est pas apparu)")
        + f" [icône avant « {state} » / après « {new_state} », compteur {before} → {after}]"
    )


FOLLOWING_RE = re.compile(r"^(Following|Abonné\(e\)|Abonné|Abonnée|Abonnés?|Suivi\(e\)|Suivi|Suivie|Requested|Demandé|Demande envoyée)", re.I)
FOLLOW_BTN_RE = re.compile(r"^(Follow|Suivre|Follow Back|Suivre en retour|S'abonner)$", re.I)


async def _buttons_seen(page: Page) -> str:
    try:
        names = await page.get_by_role("button").all_inner_texts()
        return " | ".join(dict.fromkeys(n.strip() for n in names if n.strip()))[:200] or "aucun"
    except Exception:
        return "?"


async def follow_account(page: Page, account: str) -> str:
    await page.goto(f"{IG}/{account}/", wait_until="domcontentloaded")
    await human_delay(2.5, 5)
    check_logged_in(page)
    await check_blocked(page)
    for attempt in range(2):
        try:
            if await page.get_by_role("button", name=FOLLOWING_RE).first.is_visible(timeout=1500):
                return "déjà suivi"
        except Exception:
            pass
        btn = page.get_by_role("button", name=FOLLOW_BTN_RE).first
        try:
            await btn.wait_for(state="visible", timeout=8000)
        except PWTimeout:
            if attempt == 0:                       # page parfois lente / incomplète : on recharge une fois
                await page.reload(wait_until="domcontentloaded")
                await human_delay(2.5, 4)
                continue
            raise RuntimeError(f"bouton Suivre introuvable sur @{account} (boutons vus : {await _buttons_seen(page)})")
        await btn.click()
        for _ in range(4):
            await asyncio.sleep(1.0)
            await check_blocked(page)
            try:
                if await page.get_by_role("button", name=FOLLOWING_RE).first.is_visible(timeout=500):
                    return "suivi (vérifié)"
            except Exception:
                pass
        raise RuntimeError(f"clic sur Suivre sans effet pour @{account} (boutons vus : {await _buttons_seen(page)})")
    raise RuntimeError(f"@{account} : impossible de suivre")


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
    try:
        leftover = (await box.input_value(timeout=2000)).strip()
    except Exception:
        leftover = ""
    if leftover:
        raise RuntimeError("le commentaire n'a pas été publié (le texte est resté dans la zone de saisie)")
    return "commenté (zone de saisie vidée)"
