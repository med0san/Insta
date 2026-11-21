"""Analyse locale (gratuite, sans API ni IA externe) des légendes Instagram.

Détecte les concours à partir de règles : mots-clés FR/EN, dates de fin, @mentions,
nombre d'amis à identifier, mot-clé imposé en commentaire.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any


class AnalyzerError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------
def _norm(text: str) -> str:
    """Minuscules, sans accents, apostrophes normalisées."""
    text = text.replace("’", "'").replace("‘", "'")
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


NUMBER_WORDS = {
    "un": 1, "une": 1, "one": 1, "1": 1,
    "deux": 2, "two": 2, "trois": 3, "three": 3,
    "quatre": 4, "four": 4, "cinq": 5, "five": 5,
}
NUM = r"(\d+|un|une|deux|trois|quatre|cinq|one|two|three|four|five)"

MONTHS = {
    "janvier": 1, "janv": 1, "jan": 1, "january": 1,
    "fevrier": 2, "fev": 2, "feb": 2, "february": 2,
    "mars": 3, "mar": 3, "march": 3,
    "avril": 4, "avr": 4, "apr": 4, "april": 4,
    "mai": 5, "may": 5,
    "juin": 6, "jun": 6, "june": 6,
    "juillet": 7, "juil": 7, "jul": 7, "july": 7,
    "aout": 8, "aug": 8, "august": 8,
    "septembre": 9, "sept": 9, "sep": 9, "september": 9,
    "octobre": 10, "oct": 10, "october": 10,
    "novembre": 11, "nov": 11, "november": 11,
    "decembre": 12, "dec": 12, "december": 12,
}
MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))

# ---------------------------------------------------------------------------
# Détection du concours
# ---------------------------------------------------------------------------
STRONG_RE = re.compile(
    r"\b(jeu[\s-]*concours|concours|giveaway|give[\s-]away|contest|sweepstake|tirage au sort|raffle)\b"
)
PRIZE_RE = re.compile(
    r"\b(a gagner|gagner|gagne|gagnez|remporte\w*|to win|win|prize|lot|cadeau|offert|offre)\b"
)
ACTION_RE = re.compile(
    r"\b(follow\w*|suiv\w*|suis|abonn\w*|like\w*|aime\w*|comment\w*|identifi\w*|tag\w*|"
    r"mention\w*|partag\w*|share|participe\w*|participer|enter|retweet)\b"
)
ENDED_RE = re.compile(
    r"(concours (?:est )?(?:termine|clos|cloture|fini)|giveaway (?:is )?(?:now )?(?:closed|ended|over)|"
    r"(?:le|la|les) gagnante?s? (?:est|sont|a ete|ont ete)|et le gagnant|gagnant(?:e)? du (?:concours|tirage)|"
    r"felicitations a\b|congrat\w* to|winners? (?:is|are|was|were)\b|winners? announced|"
    r"resultats? du (?:concours|tirage)|tirage au sort (?:a ete |est )?effectue|"
    r"ce concours est termine|giveaway closed|merci a tous (?:d'avoir|pour votre) particip)"
)
PAID_RE = re.compile(
    r"(achat obligatoire|achetez .{0,30} pour participer|acheter .{0,30} pour participer|"
    r"frais de participation|participation payante|purchase required|buy .{0,30} to enter)"
)

FOLLOW_RE = re.compile(r"\b(follow\w*|suiv\w*|suis|abonn\w*|subscribe)\b")
US_RE = re.compile(
    r"(notre (?:compte|page)|nous suivre|nous suivez|suis[- ]nous|suivez[- ]nous|follow us|"
    r"la page|ce compte|our (?:account|page)|abonne[- ]toi|abonnez[- ]vous)"
)
MENTION_RE = re.compile(r"@([A-Za-z0-9._]{1,30})")

TAG_VERB = r"(?:identifi\w*|tag\w*|mention\w*|invit\w*|marque\w*)"
FRIEND_WORD = r"(?:ami|pote|copain|copine|friend|personne|people|person|proche|mate)"
FRIENDS_N_RE = re.compile(
    TAG_VERB + r"\s+(?:au moins\s+|minimum\s+|at least\s+)?" + NUM +
    r"\s+(?:de\s+(?:tes|vos)\s+|(?:tes|vos|your)\s+)?" + FRIEND_WORD
)
FRIENDS_N2_RE = re.compile(NUM + r"\s+(?:de\s+(?:tes|vos)\s+|(?:tes|vos|your)\s+)?" + FRIEND_WORD + r"s?\b")
FRIENDS_ANY_RE = re.compile(TAG_VERB + r"[^.\n]{0,40}" + FRIEND_WORD)

KEYWORD_QUOTED_RE = re.compile(
    r"(?:commente\w*|ecris\w*|écris\w*|ecrire|poste\w*|reponds?\w*|réponds?\w*|laisse\w*|dis\b|dites|"
    r"comment|write|type|say|mot|word)[^\n\"«“]{0,60}[\"«“]\s*([^\"»”\n]{1,60}?)\s*[\"»”]",
    re.I,
)
KEYWORD_WORD_RE = re.compile(
    r"(?:le mot(?:[- ]cle)?|the word|mot[- ]cle|keyword|le code)\s*:?\s*([A-Za-z0-9À-ÿ!?]{2,30})",
    re.I,
)

END_CTX_RE = re.compile(
    r"(jusqu|avant le|avant minuit|au plus tard|fin le|fin du|se termine|termine le|prend fin|"
    r"cloture|clos le|until|till|through|ends?\b|ending|deadline|\bby\b|tirage|draw|limite|"
    r"valable|ouvert jusqu|date de fin)"
)

DATE_PATTERNS = [
    re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2,4}))?\b"),                          # 25/11[/2026]
    re.compile(r"\b(\d{1,2})(?:er|st|nd|rd|th)?\s+(?:de\s+)?(" + MONTH_RE + r")\b\.?(?:\s+(\d{4}))?"),  # 25 novembre
    re.compile(r"\b(" + MONTH_RE + r")\b\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?"),            # november 25
]


def _parse_post_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _build_date(day: int, month: int, year: int | None, ref: date) -> date | None:
    explicit = year is not None
    if year is not None and year < 100:
        year += 2000
    y = year or ref.year
    try:
        d = date(y, month, day)
    except ValueError:
        return None
    if not explicit and d < ref - timedelta(days=7):
        try:
            d = date(y + 1, month, day)  # "jusqu'au 5 janvier" publié en décembre
        except ValueError:
            return None
    return d


def _find_end_date(norm: str, ref: date) -> date | None:
    candidates: list[date] = []
    for idx, pattern in enumerate(DATE_PATTERNS):
        for m in pattern.finditer(norm):
            context = norm[max(0, m.start() - 45): m.start()]
            if not END_CTX_RE.search(context):
                continue
            g = m.groups()
            if idx == 0:
                day, month, year = int(g[0]), int(g[1]), int(g[2]) if g[2] else None
            elif idx == 1:
                day, month, year = int(g[0]), MONTHS[g[1]], int(g[2]) if g[2] else None
            else:
                day, month, year = int(g[1]), MONTHS[g[0]], int(g[2]) if g[2] else None
            d = _build_date(day, month, year, ref)
            if d:
                candidates.append(d)
    return max(candidates) if candidates else None


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def _count_friends(norm: str) -> int:
    m = FRIENDS_N_RE.search(norm) or FRIENDS_N2_RE.search(norm) if FRIENDS_ANY_RE.search(norm) else None
    if m:
        return min(NUMBER_WORDS.get(m.group(1)) or int(m.group(1)), 5)
    return 1 if FRIENDS_ANY_RE.search(norm) else 0


def _accounts_to_follow(caption: str, author: str | None) -> list[str]:
    if not FOLLOW_RE.search(_norm(caption)):
        return []
    accounts: list[str] = []
    needs_author = False
    for sentence in re.split(r"\n|(?<=[.!?])\s+", caption):
        ns = _norm(sentence)
        if not FOLLOW_RE.search(ns):
            continue
        if re.search(TAG_VERB + r"[^.\n]{0,30}" + FRIEND_WORD, ns):
            continue  # phrase d'identification d'amis, pas d'abonnement
        found = [h.rstrip(".") for h in MENTION_RE.findall(sentence)]
        accounts += found
        if US_RE.search(ns) or not found:
            needs_author = True
    if author and (needs_author or not accounts):
        accounts.insert(0, author)
    seen: set[str] = set()
    out = []
    for a in accounts:
        a = a.lstrip("@")
        if a and a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    return out


def _custom_keyword(caption: str) -> str | None:
    m = KEYWORD_QUOTED_RE.search(caption)
    if m:
        return m.group(1).strip()
    m = KEYWORD_WORD_RE.search(caption)
    if m:
        return m.group(1).strip()
    return None


def _prize(caption: str) -> str | None:
    m = re.search(
        r"(?:à gagner|a gagner|gagner|gagne|remporte(?:r|z)?|to win|win)\s*:?\s*([^\n.!?]{3,70})",
        caption,
        re.I,
    )
    return m.group(1).strip(" :-–—") if m else None


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------
def analyze_sync(caption: str, author: str | None = None, post_date: str | None = None) -> dict[str, Any]:
    norm = _norm(caption)
    today = date.today()
    posted = _parse_post_date(post_date)
    ref = posted or today

    result: dict[str, Any] = {
        "is_giveaway": False,
        "is_active": False,
        "accounts_to_follow": [],
        "friends_count": 0,
        "custom_keyword": None,
        "reason": "",
    }

    strong = bool(STRONG_RE.search(norm))
    prize = bool(PRIZE_RE.search(norm))
    action = bool(ACTION_RE.search(norm))

    if PAID_RE.search(norm):
        result["reason"] = "Achat ou participation payante requis : ignoré."
        return result
    if not ((strong and action) or (prize and action and (FOLLOW_RE.search(norm) or FRIENDS_ANY_RE.search(norm)))):
        result["reason"] = "Aucun indice clair de concours (mots-clés ou conditions de participation absents)."
        return result

    result["is_giveaway"] = True
    result["accounts_to_follow"] = _accounts_to_follow(caption, author)
    result["friends_count"] = _count_friends(norm)
    result["custom_keyword"] = _custom_keyword(caption)

    details = []
    prize_txt = _prize(caption)
    if prize_txt:
        details.append(f"lot : {prize_txt}")

    if ENDED_RE.search(norm):
        result["reason"] = "Concours terminé (annonce de gagnant ou de clôture)."
        return result

    end = _find_end_date(norm, ref)
    if end:
        details.append(f"fin le {end.strftime('%d/%m/%Y')}")
        if end < today:
            result["reason"] = f"Concours expiré (fin le {end.strftime('%d/%m/%Y')})."
            return result
    elif posted and (today - posted).days > 45:
        result["reason"] = f"Aucune date de fin et publication ancienne ({posted.isoformat()}) : considéré terminé."
        return result

    result["is_active"] = True
    result["reason"] = "Concours actif" + (" — " + ", ".join(details) if details else " (aucune date de fin indiquée)") + "."
    return result


async def analyze_caption(caption: str, author: str | None = None, post_date: str | None = None) -> dict[str, Any]:
    return analyze_sync(caption, author, post_date)
