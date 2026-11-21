"""Analyse des légendes Instagram : lecture des conditions de participation.

- Mode "rules" (défaut, gratuit, hors ligne) : règles FR/EN.
- Mode "ollama" (optionnel, gratuit, local) : un petit modèle tourne sur votre PC via Ollama, lit la
  légende ET l'image du post, et rédige la réponse aux questions. Repli automatique sur les règles.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import unicodedata
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any

from logbus import bus


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
    r"\b(follow\w*|suiv\w*|suis|abonn\w*|like\w*|aime\w*|liker|comment\w*|identifi\w*|tag\w*|"
    r"mention\w*|partag\w*|share|participe\w*|participer|enter|retweet|enregistr\w*)\b"
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
LIKE_RE = re.compile(r"\b(like\w*|liker|likez|aime\w*|j'aime)\b")
SAVE_RE = re.compile(r"\b(enregistr\w*|sauvegard\w*|save|bookmark)\b")
COMMENT_RE = re.compile(r"\b(commente\w*|commentaires?|commentez|en commentaire|in the comments?|comment below|comment on)\b")

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

# Conditions que le bot ne sait pas remplir (sauf si elles sont présentées comme bonus/optionnelles)
OPTIONAL_RE = re.compile(
    r"\b(bonus|optionnel\w*|facultatif\w*|en plus|chances? supplementaires?|augmenter|booster|multiplier|"
    r"peux|peut|pouvez|possibilite|optional|extra|can also|boost|pas besoin|inutile|no need|sans obligation)\b"
)
STORY_RE = re.compile(
    r"(\b(?:stories|story|storie|repost\w*|republi\w*)\b|"
    r"\bpartag\w*\s+(?:cette|ce|cet|le|la|l')\s*(?:publication|post|reel|video|giveaway|concours)|"
    r"\bshare (?:this|the) (?:post|reel|giveaway))"
)
DM_RE = re.compile(r"(\ben (?:mp|dm)\b|message prive|\bdm (?:us|moi|nous)\b|\benvoie\w*[^.\n]{0,30}\b(?:mp|dm)\b)")
NEGATION_RE = re.compile(r"\b(pas besoin|inutile|no need|pas obligatoire|non obligatoire|sans obligation)\b")
OTHER_PLATFORM_RE = re.compile(
    r"\b(tiktok|youtube|facebook|twitter|snapchat|twitch|linkedin|pinterest|newsletter|telegram|discord|whatsapp)\b"
)
JOIN_RE = re.compile(r"\b(inscri\w*|rejoin\w*|join)\b")

# « Suivre les comptes identifiés sur la photo » : comptes tagués dans l'image, absents de la légende
TAGGED_RE = re.compile(
    r"(\b(?:comptes?|marques?|partenaires?|pages?|profils?|createur\w*|createurs?)\b[^.\n]{0,25}"
    r"\b(?:identifi|tagu|tagg|mentionn)\w*[^.\n]{0,25}\b(?:photo|publication|post|image|visuel|carrousel|video|reel)\b)"
    r"|(\b(?:identifi|tagu|tagg)\w*\s+(?:sur|en|dans)\s+(?:la |le |l'|cette |ce )?(?:photo|publication|post|image|visuel|carrousel)\b)"
)

# Vraie question à laquelle répondre, ou simple slogan d'accroche (« Envie de gagner … ? ») ?
EXPLICIT_ASK_RE = re.compile(
    r"(\b(?:dis|dites)[- ](?:moi|nous)\b|\b(?:me|nous) (?:dire|disant)\b|\bdire[- ](?:moi|nous)\b|\btell (?:me|us)\b|"
    r"\braconte\w*|\breponds?\b|\brepondez\b|\brepondre\b|\bexplique\w*|\banswer\b|"
    r"en commentaire[^.!?\n]{0,60}\b(?:quel\w*|qui|pourquoi|ce que|ton|ta|tes|votre|vos)\b)"
)
WH_RE = re.compile(r"\b(quel\w*|lequel|laquelle|lesquel\w*|pourquoi|combien|avec qui|which|what|why|who)\b")
HOOK_RE = re.compile(
    r"^(?:et )?(envie|tu veux|vous voulez|ca te dit|ca vous dit|qui veut|qui a envie|pret|prete|prets|pretes|alors|"
    r"et si|vous aussi|toi aussi|besoin|marre|devinez|qui n'a|vous revez|tu reves|on vous|quoi de mieux|"
    r"want|ready|who wants)\b"
)
WIN_RE = re.compile(r"\b(gagner|remporter|tenter|win|gagnez|gagne|remportez)\b")
KW_LEAD_RE = re.compile(
    r"(?:comment\w*|ecri\w*|tape\w*|ajoute\w*|poste\w*|laisse\w*|write|type|say|mot[- ]cle|\bmot\b|keyword)"
    r"(?:\s*(?:le mot(?:[- ]cle)?|avec|the word|:|par|juste|l'emoji|un emoji|l'emoji suivant|simplement|uniquement))*\s*:?\s*[\"«“']?\s*$"
)

# Question à laquelle il faut répondre en commentaire
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


def _find_end_date(norm: str, ref: date) -> tuple[date, str] | None:
    """Date de fin la plus tardive et l'extrait de légende dont elle provient."""
    candidates: list[tuple[date, str]] = []
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
                snippet = re.sub(r"\s+", " ", norm[max(0, m.start() - 30): m.end()]).strip()
                candidates.append((d, snippet))
    return max(candidates, key=lambda c: c[0]) if candidates else None


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def _count_friends(norm: str) -> int:
    m = FRIENDS_N_RE.search(norm) or FRIENDS_N2_RE.search(norm) if FRIENDS_ANY_RE.search(norm) else None
    if m:
        return min(NUMBER_WORDS.get(m.group(1)) or int(m.group(1)), 5)
    return 1 if FRIENDS_ANY_RE.search(norm) else 0


def _split_sentences(caption: str) -> list[str]:
    return [s.strip() for s in re.split(r"\n|(?<=[.!?])\s+", caption) if len(s.strip()) >= 4]


def _accounts_to_follow(caption: str, author: str | None, collaborators: list[str] | None = None) -> list[str]:
    if not FOLLOW_RE.search(_norm(caption)):
        return []
    accounts: list[str] = []
    needs_author = False
    for sentence in _split_sentences(caption):
        ns = _norm(sentence)
        if not FOLLOW_RE.search(ns):
            continue
        if re.search(TAG_VERB + r"[^.\n]{0,30}" + FRIEND_WORD, ns):
            continue  # phrase d'identification d'amis, pas d'abonnement
        if OTHER_PLATFORM_RE.search(ns):
            continue  # abonnement hors Instagram
        found = [h.rstrip(".") for h in MENTION_RE.findall(sentence)]
        accounts += found
        if US_RE.search(ns) or not found:
            needs_author = True
    if author and (needs_author or not accounts):
        accounts.insert(0, author)
        accounts[1:1] = list(collaborators or [])  # post en collaboration : « nous » = tous les co-auteurs
    seen: set[str] = set()
    out = []
    for a in accounts:
        a = a.lstrip("@")
        if a and a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    return out


KEYWORD_DIRECT_RE = re.compile(
    r"\b(?i:commente[sz]?|comment|écris|ecris|écrivez|tape[sz]?)\b\s*(?i:avec\s*)?(?i:l'emoji|un emoji|l’emoji|le mot|le mot[- ]clé|le mot[- ]cle)?\s*:?\s*"
    r"(?:([^\w\s@#,.;:!?()\"'«»“”-]{1,8})|\b([A-ZÀ-Ý][A-ZÀ-Ý0-9]{2,19})\b)"
)


def _custom_keyword(caption: str) -> str | None:
    m = KEYWORD_QUOTED_RE.search(caption)
    if m:
        return m.group(1).strip()
    m = KEYWORD_WORD_RE.search(caption)
    if m:
        return m.group(1).strip()
    # « commente ❄️ », « commente avec l'emoji 🎄 », « commente OUI »
    m = KEYWORD_DIRECT_RE.search(caption)
    if m:
        return (m.group(1) or m.group(2)).strip()
    return None


def _prize(caption: str) -> str | None:
    m = re.search(
        r"(?:à gagner|a gagner|pour gagner|gagner|gagnez|gagne|remporter|to win)\s*:?\s*([^\n.!?,@#(]{4,60})",
        caption,
        re.I,
    )
    if not m:
        return None
    txt = re.sub(r"\s+\S{1,2}$", "", m.group(1).strip(" :-–—")).strip()
    return txt if len(txt) >= 4 and re.match(r"\w", txt) else None


def _choice_from_question(question: str) -> str | None:
    """Pour une question du type « Team chat ou chien ? » : choisit une des deux options."""
    tail = question.split(":")[-1].strip(" ?!.")
    parts = [p.strip(" ,;?!.") for p in re.split(r"\s+ou\s+|\s+or\s+", tail, flags=re.I)]
    if len(parts) == 2 and all(1 <= len(p) <= 30 for p in parts):
        return random.choice(parts)
    return None


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", _norm(text))


def _is_real_question(sentence: str) -> bool:
    """Vraie question à laquelle répondre en commentaire (pas un slogan rhétorique)."""
    ns = re.sub(r"^[^\w]+", "", _norm(sentence))
    if EXPLICIT_ASK_RE.search(ns):
        return True
    if "?" not in sentence or HOOK_RE.match(ns) or WIN_RE.search(ns):
        return False
    return bool(WH_RE.search(ns)) or " ou " in ns


def _quote_in_caption(quote: str, caption: str) -> bool:
    """La citation renvoyée par le modèle figure-t-elle vraiment dans la légende ?"""
    qw = [w for w in _words(quote) if len(w) > 2]
    if not qw:
        return False
    cw = set(_words(caption))
    return sum(w in cw for w in qw) / len(qw) >= 0.7


def _valid_keyword(kw: str | None, caption: str) -> str | None:
    """Garde le mot-clé seulement s'il est imposé par une consigne (« commente X », guillemets…)."""
    kw = (kw or "").strip().strip("\"'«»“” ")
    if not kw or len(kw) > 60 or kw.startswith("@"):
        return None
    nc, nk = _norm(caption), _norm(kw)
    for m in re.finditer(re.escape(nk), nc):
        before = nc[max(0, m.start() - 80): m.start()]
        if re.search(r"[\"«“]\s*$", before) or KW_LEAD_RE.search(before):
            return kw
    return None


def _valid_answer(ans: str | None, caption: str) -> str | None:
    """Rejette une « réponse » qui recopie la légende ou qui est elle-même une question."""
    if not ans:
        return None
    ans = MENTION_RE.sub("", str(ans))
    ans = re.sub(r"#\w+", "", ans)
    ans = re.sub(r"\s+", " ", ans).strip(" \"'«»“”")
    if len(ans) < 2 or len(ans) > 220 or ans.endswith("?"):
        return None
    aw, cap = _words(ans), " ".join(_words(caption))
    for i in range(max(0, len(aw) - 4)):
        if " ".join(aw[i:i + 5]) in cap:
            return None  # 5 mots consécutifs copiés de la légende
    return ans


RULE_ANSWERS = [
    (re.compile(r"\bavec qui\b"), "Avec ma famille, sans hésiter ! 🥰"),
    (re.compile(r"\ba qui\b"), "À ma maman, elle le mérite tellement ! 🥰"),
    (re.compile(r"\bpourquoi\b|\bwhy\b"), "Parce que ça me ferait vraiment plaisir ! 😍"),
    (re.compile(r"\bquand\b"), "Pour les fêtes de fin d'année ! 🎄"),
]


def _rule_answer(question: str) -> str | None:
    """Réponse simple pour quelques formes de question courantes (mode sans IA)."""
    nq = _norm(question)
    for pattern, answer in RULE_ANSWERS:
        if pattern.search(nq):
            return answer
    return None


def _scan_conditions(caption: str, keyword: str | None) -> tuple[list[str], str | None, bool]:
    """Conditions impossibles pour le bot, question à laquelle répondre, et story/republication demandée."""
    unsupported: list[str] = []
    question: str | None = None
    needs_story = False
    for s in _split_sentences(caption):
        ns = _norm(s)
        optional = bool(OPTIONAL_RE.search(ns))
        if STORY_RE.search(ns) and not NEGATION_RE.search(ns):
            needs_story = True  # faisable : republication / story (obligatoire ou bonus)
        if not optional:
            if DM_RE.search(ns):
                unsupported.append(f"envoi en message privé obligatoire (phrase : « {s[:100]} »)")
            else:
                m = OTHER_PLATFORM_RE.search(ns)
                if m and (FOLLOW_RE.search(ns) or JOIN_RE.search(ns)):
                    unsupported.append(f"abonnement obligatoire sur une autre plateforme ({m.group(1)}) (phrase : « {s[:100]} »)")
        # « Team chat ou chien ? » : question à choix ; les slogans rhétoriques (« Envie de … ? ») sont ignorés
        if question is None and _is_real_question(s):
            clean = re.sub(r"\([^)]*\)", "", s)                 # retire les parenthèses « (1 commentaire = 1 chance) »
            clean = re.sub(r"^[^\w«\"“]+", "", clean)            # retire puces / emojis en début de phrase
            question = re.sub(r"\s+", " ", clean).strip()[:200]
    return list(dict.fromkeys(unsupported)), question, needs_story


def _conditions(result: dict[str, Any], norm: str) -> list[str]:
    out = []
    if result["accounts_to_follow"]:
        out.append("suivre " + ", ".join("@" + a for a in result["accounts_to_follow"]))
    if LIKE_RE.search(norm):
        out.append("liker")
    if result.get("follow_tagged"):
        out.append("suivre les comptes identifiés sur la photo"
                   + (f" ({len(result.get('tagged') or [])} trouvé(s))" if result.get("tagged") else " (aucun trouvé)"))
    if result["needs_save"]:
        out.append("enregistrer le post")
    if result["needs_story"]:
        out.append("republier / mettre en story")
    if result["friends_count"]:
        out.append(f"identifier {result['friends_count']} ami(s)")
    if result["custom_keyword"]:
        out.append(f"écrire « {result['custom_keyword']} »")
    if result["question"]:
        out.append(f"répondre à : {result['question']}")
    for u in result["unsupported"]:
        out.append(f"⚠ {u}")
    return out


def _activity(caption: str, post_date: str | None, details: list[str]) -> tuple[bool, str]:
    """(terminé ?, raison) d'après les annonces de gagnants et les dates de fin — déterministe."""
    norm = _norm(caption)
    today = date.today()
    posted = _parse_post_date(post_date)
    if ENDED_RE.search(norm):
        return True, "Concours terminé (annonce de gagnant ou de clôture)."
    found = _find_end_date(norm, posted or today)
    if found:
        end, snippet = found
        details.append(f"fin le {end.strftime('%d/%m/%Y')}")
        if end < today:
            return True, (
                f"Concours expiré : fin le {end.strftime('%d/%m/%Y')} (déjà passée au {today.strftime('%d/%m/%Y')}), "
                f"d'après l'extrait « …{snippet}… »."
            )
    elif posted and (today - posted).days > 45:
        return True, f"Aucune date de fin et publication ancienne ({posted.isoformat()}) : considéré terminé."
    return False, ""


# ---------------------------------------------------------------------------
# Analyse par règles
# ---------------------------------------------------------------------------
def analyze_sync(
    caption: str,
    author: str | None = None,
    post_date: str | None = None,
    collaborators: list[str] | None = None,
    tagged: list[str] | None = None,
) -> dict[str, Any]:
    norm = _norm(caption)

    result: dict[str, Any] = {
        "is_giveaway": False,
        "is_active": False,
        "accounts_to_follow": [],
        "friends_count": 0,
        "custom_keyword": None,
        "needs_save": False,
        "needs_comment": False,
        "needs_story": False,
        "follow_tagged": False,
        "tagged": list(tagged or []),
        "question": None,
        "answer": None,
        "unsupported": [],
        "conditions": [],
        "reason": "",
        "_hard_inactive": False,
        "_details": [],
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
    result["accounts_to_follow"] = _accounts_to_follow(caption, author, collaborators)
    result["follow_tagged"] = bool(TAGGED_RE.search(norm))
    if result["follow_tagged"]:
        for t in result["tagged"]:
            if t.lower() not in {a.lower() for a in result["accounts_to_follow"]}:
                result["accounts_to_follow"].append(t)
    result["friends_count"] = _count_friends(norm)
    result["custom_keyword"] = _custom_keyword(caption)
    result["needs_save"] = bool(SAVE_RE.search(norm))
    result["needs_comment"] = bool(COMMENT_RE.search(norm)) or result["friends_count"] > 0
    result["unsupported"], result["question"], result["needs_story"] = _scan_conditions(caption, result["custom_keyword"])
    if result["question"]:
        result["answer"] = _choice_from_question(result["question"]) or _rule_answer(result["question"])
    result["conditions"] = _conditions(result, norm)

    details = result["_details"]
    prize_txt = _prize(caption)
    if prize_txt:
        details.append(f"lot : {prize_txt}")

    ended, why = _activity(caption, post_date, details)
    if ended:
        result["_hard_inactive"] = True
        result["reason"] = why
        return result

    result["is_active"] = True
    result["reason"] = _active_reason(details, result["conditions"])
    return result


def _active_reason(details: list[str], conditions: list[str]) -> str:
    reason = "Concours actif" + (" — " + ", ".join(details) if details else " (aucune date de fin indiquée)") + "."
    if conditions:
        reason += " À faire : " + " ; ".join(conditions) + "."
    return reason


# ---------------------------------------------------------------------------
# Modèle d'IA (Ollama local ou API gratuite) : extraction des conditions, puis réponse à la question
# ---------------------------------------------------------------------------
LLM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_giveaway": {"type": "boolean"},
        "accounts_to_follow": {"type": "array", "items": {"type": "string"}},
        "follow_tagged_accounts": {"type": "boolean"},
        "friends_count": {"type": "integer"},
        "comment_keyword": {"type": "string"},
        "question": {"type": "string"},
        "needs_save": {"type": "boolean"},
        "needs_story": {"type": "boolean"},
        "unsupported_conditions": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": [
        "is_giveaway", "accounts_to_follow", "follow_tagged_accounts", "friends_count", "comment_keyword",
        "question", "needs_save", "needs_story", "unsupported_conditions", "reason",
    ],
}

LLM_SYSTEM = """Tu es un expert des concours Instagram. Lis TOUTE la légende (et l'image si fournie) et liste précisément CE QU'IL FAUT FAIRE pour participer. Ne devine rien : n'extrais que ce qui est écrit.
Réponds uniquement avec l'objet JSON demandé.

- is_giveaway : vrai s'il y a un lot à gagner (faux pour une annonce de gagnant ou un achat obligatoire).
- accounts_to_follow : TOUS les pseudos Instagram (sans @) qu'il faut suivre / auxquels il faut s'abonner : organisateur, partenaires, compte de secours. Si la légende dit « suivre notre compte / nous suivre », mets le compte auteur et les co-auteurs fournis.
- follow_tagged_accounts : vrai si la légende demande de suivre « les comptes identifiés / tagués sur la photo / la publication ».
- friends_count : nombre d'amis à identifier en commentaire (0 si aucun ; « identifie tes amis » sans nombre = 1).
- comment_keyword : mot, phrase ou emoji IMPOSÉ à écrire en commentaire (ex : « commente PARTICIPE », « commente avec 🎄 »). Copie-le exactement. Chaîne vide s'il n'y en a pas. Ce n'est PAS une phrase de la légende ni la réponse à une question.
- question : copie EXACTE de la question à laquelle il faut répondre en commentaire (ex : « dis-moi quel cadeau te fait envie »). Une accroche comme « Envie de gagner … ? », « Prêt à … ? » ou « Et si … ? » n'est PAS une question à répondre. Chaîne vide sinon.
- needs_save : vrai s'il faut enregistrer la publication.
- needs_story : vrai si la légende parle de partager / republier en story (obligatoire ou bonus).
- unsupported_conditions : conditions OBLIGATOIRES impossibles à automatiser : message privé, abonnement TikTok/YouTube/Facebook/newsletter, achat. La story / republication est faisable : ne la mets PAS ici.
- reason : une phrase courte en français."""

ANSWER_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
ANSWER_SYSTEM = """Tu participes à un concours Instagram et tu dois répondre en commentaire à la question de l'organisateur.
Écris UNE réponse courte (moins de 20 mots), naturelle, sincère, enthousiaste, en français, à la première personne, qui répond VRAIMENT à la question (si on te demande de choisir un lot, choisis-en un en le nommant ; si on te demande avec qui tu partagerais le lot, réponds « avec ma famille », etc.).
N'écris ni @, ni hashtag, ne recopie pas la légende et ne pose pas de question.
Réponds uniquement en JSON : {"answer": "..."}"""


def _schema_hint(schema: dict[str, Any]) -> str:
    kinds = {"boolean": "bool", "integer": "entier", "string": "chaîne", "array": "liste de chaînes"}
    keys = ", ".join(f"{k} ({kinds.get(v.get('type'), 'valeur')})" for k, v in schema["properties"].items())
    return f"\n\nRéponds avec UN SEUL objet JSON ayant exactement ces clés : {keys}. Aucun texte hors du JSON."


async def _llm_json(cfg: dict[str, Any], system: str, text: str, image_b64: str | None, schema: dict[str, Any]) -> dict[str, Any]:
    """Appel au modèle choisi (Ollama local ou API compatible OpenAI) et lecture du JSON renvoyé."""
    if cfg.get("analyzer_mode") == "ollama":
        message: dict[str, Any] = {"role": "user", "content": text}
        if image_b64:
            message["images"] = [image_b64]
        payload = {
            "model": cfg.get("ollama_model") or "gemma3:4b",
            "stream": False,
            "format": schema,
            "options": {"temperature": 0.2},
            "messages": [{"role": "system", "content": system}, message],
        }
        data = await asyncio.to_thread(_ollama_chat, cfg.get("ollama_url") or "http://localhost:11434", payload, 240)
        return _extract_json(data["message"]["content"])

    content: Any = text
    if image_b64:
        content = [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image_b64}},
        ]
    payload = {
        "model": cfg.get("api_model") or "gemini-2.5-flash",
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system + _schema_hint(schema)}, {"role": "user", "content": content}],
    }
    if "generativelanguage.googleapis.com" in (cfg.get("api_base_url") or ""):
        payload["reasoning_effort"] = "low"  # Gemini « réfléchit » sinon longuement : inutile ici
    data = await _api_request(cfg, payload)
    return _extract_json(data["choices"][0]["message"].get("content") or "")


async def _llm_analyze(caption, author, post_date, image_b64, cfg, collaborators=None, tagged=None) -> dict[str, Any]:
    text = (
        f"Date du jour : {date.today().isoformat()}\n"
        f"Compte auteur : {('@' + author) if author else 'inconnu'}\n"
        + (f"Co-auteurs du post : {', '.join('@' + c for c in collaborators)}\n" if collaborators else "")
        + (f"Comptes identifiés sur la photo : {', '.join('@' + t for t in tagged)}\n" if tagged else "")
        + f"Date de publication : {post_date or 'inconnue'}\n\nLégende :\n\"\"\"\n{caption[:4000]}\n\"\"\""
    )
    return await _llm_json(cfg, LLM_SYSTEM, text, image_b64, LLM_SCHEMA)


async def _llm_answer(question: str, caption: str, image_b64: str | None, cfg: dict[str, Any]) -> str | None:
    text = f"Question de l'organisateur : « {question} »\n\nLégende du concours (contexte) :\n\"\"\"\n{caption[:2500]}\n\"\"\""
    data = await _llm_json(cfg, ANSWER_SYSTEM, text, image_b64, ANSWER_SCHEMA)
    return _valid_answer(data.get("answer"), caption)


def _ollama_chat(base_url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ollama_status(base_url: str, model: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=4) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"Ollama injoignable sur {base_url} ({e}). Lancez l'application Ollama."}
    names = [m.get("name", "") for m in tags.get("models", [])]
    present = any(n == model or n.split(":")[0] == model.split(":")[0] and ":" not in model for n in names)
    if not present:
        return {"ok": False, "models": names, "message": f"Modèle « {model} » absent. Installez-le : ollama pull {model}"}
    return {"ok": True, "models": names, "message": f"Ollama prêt, modèle « {model} » disponible."}


def _merge(rules: dict[str, Any], llm: dict[str, Any], caption: str, author: str | None,
           post_date: str | None = None, collaborators: list[str] | None = None,
           tagged: list[str] | None = None) -> dict[str, Any]:
    low_caption = caption.lower()
    known = {h.lower() for h in [author or "", *(collaborators or []), *(tagged or [])] if h}
    accounts = list(rules["accounts_to_follow"])
    for a in llm.get("accounts_to_follow") or []:
        a = str(a).strip().lstrip("@").rstrip(".")
        # pseudos inventés par le modèle écartés : seuls ceux présents dans la légende / la page sont gardés
        if a and (a.lower() in known or "@" + a.lower() in low_caption) and a.lower() not in {x.lower() for x in accounts}:
            accounts.append(a)
    follow_tagged = rules["follow_tagged"] or bool(llm.get("follow_tagged_accounts"))
    if follow_tagged:
        for t in tagged or []:
            if t.lower() not in {x.lower() for x in accounts}:
                accounts.append(t)
    norm = _norm(caption)
    try:
        llm_friends = int(llm.get("friends_count") or 0)
    except (TypeError, ValueError):
        llm_friends = 0
    # le modèle ne peut ajouter des amis / une story / un enregistrement que si la légende en parle vraiment
    friends = max(llm_friends if re.search(TAG_VERB, norm) else 0, rules["friends_count"])

    keyword = rules["custom_keyword"] or _valid_keyword(llm.get("comment_keyword"), caption)
    llm_q = str(llm.get("question") or "").strip()
    if llm_q and not (_quote_in_caption(llm_q, caption) and _is_real_question(llm_q)):
        llm_q = ""  # accroche rhétorique ou citation inventée
    question = rules["question"] or (llm_q[:200] or None)
    if keyword and question and _norm(keyword) in _norm(question) and not rules["custom_keyword"]:
        keyword = None  # « noire ou rose » : option de la question, pas un mot imposé

    unsupported = [
        u for u in rules["unsupported"] + [str(x) for x in llm.get("unsupported_conditions") or []]
        if u.strip() and not STORY_RE.search(_norm(u))
    ]
    unsupported = list(dict.fromkeys(unsupported))

    is_giveaway = bool(llm.get("is_giveaway")) or rules["is_giveaway"]
    details = list(rules["_details"])
    if rules["is_giveaway"]:
        hard, why = rules["_hard_inactive"], rules["reason"]
    else:
        hard, why = _activity(caption, post_date, details)
    out = dict(rules)
    out.update(
        is_giveaway=is_giveaway,
        # Dates de fin et annonces de gagnants : jugées par des règles déterministes (un petit modèle répond
        # souvent « inactif » à tort).
        is_active=is_giveaway and not hard,
        _hard_inactive=hard,
        accounts_to_follow=accounts,
        follow_tagged=follow_tagged,
        tagged=list(tagged or []),
        friends_count=max(0, min(friends, 10)),
        custom_keyword=keyword,
        needs_save=rules["needs_save"] or (bool(llm.get("needs_save")) and bool(SAVE_RE.search(norm))),
        needs_story=rules["needs_story"] or (bool(llm.get("needs_story")) and bool(STORY_RE.search(norm))
                                             and not NEGATION_RE.search(norm)),
        needs_comment=rules["needs_comment"] or friends > 0 or bool(question) or bool(keyword),
        question=question,
        answer=rules["answer"] if question == rules["question"] else None,
        unsupported=unsupported,
    )
    out["conditions"] = _conditions(out, _norm(caption))
    if hard:
        out["reason"] = why
    elif is_giveaway:
        out["reason"] = _active_reason(details, out["conditions"])
    return out


# ---------------------------------------------------------------------------
# Mode optionnel : API gratuite compatible OpenAI (Gemini, Groq, OpenRouter, Pollinations…)
# ---------------------------------------------------------------------------
API_MIN_INTERVAL = 4.5          # secondes entre deux appels (les offres gratuites limitent les requêtes/minute)
_api_lock = asyncio.Lock()
_api_last = 0.0


def _extract_json(text: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError(f"réponse sans JSON : {text[:120]!r}")
        return json.loads(m.group(0))


def _api_chat(base_url: str, api_key: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "User-Agent": "GiveawayBot/1.0"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_detail(e: urllib.error.HTTPError) -> str:
    try:
        body = e.read().decode("utf-8", "replace")[:200].replace("\n", " ")
    except Exception:  # noqa: BLE001
        body = ""
    return f"HTTP {e.code} {body}".strip()


async def _api_request(cfg: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Appel avec limitation de débit, 1 nouvel essai sur 429, et repli sans image/JSON-mode sur 400."""
    global _api_last
    base, key = cfg.get("api_base_url") or "", cfg.get("api_key") or ""
    async with _api_lock:
        wait = API_MIN_INTERVAL - (asyncio.get_running_loop().time() - _api_last)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            for attempt in range(3):
                try:
                    return await asyncio.to_thread(_api_chat, base, key, payload, 90)
                except urllib.error.HTTPError as e:
                    if e.code in (500, 502, 503, 504) and attempt == 0:
                        bus.log(f"API : service momentanément surchargé ({e.code}), nouvel essai dans 6s.", "warning")
                        await asyncio.sleep(6)
                        continue
                    if e.code == 429 and attempt == 0:
                        delay = min(float(e.headers.get("Retry-After") or 15), 30)
                        bus.log(f"API : limite atteinte (429), nouvel essai dans {delay:.0f}s.", "warning")
                        await asyncio.sleep(delay)
                        continue
                    if e.code == 400 and attempt <= 1:
                        # le modèle refuse peut-être l'image ou response_format : on simplifie
                        payload = json.loads(json.dumps(payload))
                        payload.pop("response_format", None)
                        payload.pop("reasoning_effort", None)
                        for m in payload["messages"]:
                            if isinstance(m.get("content"), list):
                                m["content"] = "\n".join(c.get("text", "") for c in m["content"] if c.get("type") == "text")
                        bus.log("API : requête refusée (400), nouvel essai sans image ni mode JSON.", "debug")
                        continue
                    raise
            raise AnalyzerError("API : échec après plusieurs essais.")
        finally:
            _api_last = asyncio.get_running_loop().time()


async def api_status(cfg: dict[str, Any]) -> dict[str, Any]:
    if not cfg.get("api_base_url"):
        return {"ok": False, "message": "Adresse de l'API manquante."}
    payload = {
        "model": cfg.get("api_model") or "",
        "max_tokens": 200,
        "messages": [{"role": "user", "content": "Réponds juste : OK"}],
    }
    if "generativelanguage.googleapis.com" in (cfg.get("api_base_url") or ""):
        payload["reasoning_effort"] = "none"
    try:
        data = await _api_request(cfg, payload)
        reply = str(data["choices"][0]["message"].get("content") or "").strip()[:40] or "(vide)"
        return {"ok": True, "message": f"API opérationnelle (réponse : « {reply} »)."}
    except urllib.error.HTTPError as e:
        hint = " Clé API refusée ou manquante." if e.code in (401, 403) else ""
        return {"ok": False, "message": f"{_http_detail(e)}.{hint}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"API injoignable : {e}"}


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------
async def analyze_caption(
    caption: str,
    author: str | None = None,
    post_date: str | None = None,
    image_b64: str | None = None,
    cfg: dict[str, Any] | None = None,
    collaborators: list[str] | None = None,
    tagged: list[str] | None = None,
) -> dict[str, Any]:
    rules = analyze_sync(caption, author, post_date, collaborators, tagged)
    cfg = cfg or {}
    mode = cfg.get("analyzer_mode")
    if mode not in ("ollama", "api"):
        return rules
    label = "API" if mode == "api" else "Ollama"
    try:
        llm = await _llm_analyze(caption, author, post_date, image_b64, cfg, collaborators, tagged)
        result = _merge(rules, llm, caption, author, post_date, collaborators, tagged)
    except urllib.error.HTTPError as e:
        bus.log(f"{label} en erreur ({_http_detail(e)}) : repli sur l'analyse par règles.", "warning")
        return rules
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError, TimeoutError, AnalyzerError) as e:
        bus.log(f"{label} indisponible ({e}) : repli sur l'analyse par règles.", "warning")
        return rules

    # 2e appel, dédié, pour rédiger la réponse à la vraie question (bien plus fiable qu'un seul appel « tout-en-un »)
    if result["is_active"] and result["question"] and not result["answer"]:
        try:
            result["answer"] = await _llm_answer(result["question"], caption, image_b64, cfg)
            if not result["answer"]:
                bus.log("Réponse du modèle rejetée (copie de la légende ou hors sujet).", "warning")
        except Exception as e:  # noqa: BLE001
            bus.log(f"{label} : réponse à la question impossible ({e}).", "warning")
    return result
