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
    r"(\b(?:stories|story|storie|repost\w*|republi\w*)\b|\ben (?:mp|dm)\b|message prive|\bdm (?:us|moi|nous)\b|"
    r"\bpartag\w*\s+(?:cette|ce|cet|le|la|l')\s*(?:publication|post|reel|video|giveaway|concours)|"
    r"\bshare (?:this|the) (?:post|reel|giveaway))"
)
OTHER_PLATFORM_RE = re.compile(
    r"\b(tiktok|youtube|facebook|twitter|snapchat|twitch|linkedin|pinterest|newsletter|telegram|discord|whatsapp)\b"
)
JOIN_RE = re.compile(r"\b(inscri\w*|rejoin\w*|join)\b")

# Question à laquelle il faut répondre en commentaire
QUESTION_RE = re.compile(
    r"(\b(?:dis|dites)[- ](?:moi|nous)\b|\b(?:me|nous) dire\b|\bdire[- ](?:moi|nous)\b|\btell (?:me|us)\b|"
    r"\braconte\w*|\breponds?\w*|\brepondez\b|\brepondre\b|\bexplique\w*|\banswer\b|\bpourquoi\b|\bwhy\b|"
    r"\bquel(?:le)?s?\b[^.\n?]{0,40}\b(?:te|vous|tu|ton|ta|tes|vos)\b|"
    r"\bwhich\b[^.\n?]{0,40}\b(?:you|your)\b|\bwhat\b[^.\n?]{0,30}\byour\b)"
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


def _split_sentences(caption: str) -> list[str]:
    return [s.strip() for s in re.split(r"\n|(?<=[.!?])\s+", caption) if len(s.strip()) >= 4]


def _accounts_to_follow(caption: str, author: str | None) -> list[str]:
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
    if not m:
        return None
    return re.sub(r"\s+\S{1,2}$", "", m.group(1).strip(" :-–—")).strip() or None


def _choice_from_question(question: str) -> str | None:
    """Pour une question du type « Team chat ou chien ? » : choisit une des deux options."""
    tail = question.split(":")[-1].strip(" ?!.")
    parts = [p.strip(" ,;?!.") for p in re.split(r"\s+ou\s+|\s+or\s+", tail, flags=re.I)]
    if len(parts) == 2 and all(1 <= len(p) <= 30 for p in parts):
        return random.choice(parts)
    return None


def _scan_conditions(caption: str, keyword: str | None) -> tuple[list[str], str | None]:
    """Repère les conditions impossibles pour le bot et la question à laquelle répondre."""
    unsupported: list[str] = []
    question: str | None = None
    for s in _split_sentences(caption):
        ns = _norm(s)
        optional = bool(OPTIONAL_RE.search(ns))
        if not optional:
            if STORY_RE.search(ns):
                unsupported.append("partage en story / republication / message privé obligatoire")
            else:
                m = OTHER_PLATFORM_RE.search(ns)
                if m and (FOLLOW_RE.search(ns) or JOIN_RE.search(ns)):
                    unsupported.append(f"abonnement obligatoire sur une autre plateforme ({m.group(1)})")
        is_question = bool(QUESTION_RE.search(ns)) or (
            s.rstrip().endswith("?") and not re.search(r"participer|gagner|how to|comment faire|concours|giveaway", ns)
        )
        if question is None and is_question and not (keyword and keyword.lower() in s.lower()):
            clean = re.sub(r"\([^)]*\)", "", s)                 # retire les parenthèses « (1 commentaire = 1 chance) »
            clean = re.sub(r"^[^\w«\"“]+", "", clean)            # retire puces / emojis en début de phrase
            question = re.sub(r"\s+", " ", clean).strip()[:200]
    return list(dict.fromkeys(unsupported)), question


# ---------------------------------------------------------------------------
# Analyse par règles
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
        "needs_save": False,
        "needs_comment": False,
        "question": None,
        "answer": None,
        "unsupported": [],
        "conditions": [],
        "reason": "",
        "_hard_inactive": False,
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
    result["needs_save"] = bool(SAVE_RE.search(norm))
    result["needs_comment"] = bool(COMMENT_RE.search(norm)) or result["friends_count"] > 0
    result["unsupported"], result["question"] = _scan_conditions(caption, result["custom_keyword"])
    if result["question"]:
        result["answer"] = _choice_from_question(result["question"])

    conditions = []
    if result["accounts_to_follow"]:
        conditions.append("suivre " + ", ".join("@" + a for a in result["accounts_to_follow"]))
    if LIKE_RE.search(norm):
        conditions.append("liker")
    if result["needs_save"]:
        conditions.append("enregistrer le post")
    if result["friends_count"]:
        conditions.append(f"identifier {result['friends_count']} ami(s)")
    if result["custom_keyword"]:
        conditions.append(f"écrire « {result['custom_keyword']} »")
    if result["question"]:
        conditions.append(f"répondre à : {result['question']}")
    for u in result["unsupported"]:
        conditions.append(f"⚠ {u}")
    result["conditions"] = conditions

    details = []
    prize_txt = _prize(caption)
    if prize_txt:
        details.append(f"lot : {prize_txt}")

    if ENDED_RE.search(norm):
        result["reason"] = "Concours terminé (annonce de gagnant ou de clôture)."
        result["_hard_inactive"] = True
        return result

    end = _find_end_date(norm, ref)
    if end:
        details.append(f"fin le {end.strftime('%d/%m/%Y')}")
        if end < today:
            result["reason"] = f"Concours expiré (fin le {end.strftime('%d/%m/%Y')})."
            result["_hard_inactive"] = True
            return result
    elif posted and (today - posted).days > 45:
        result["reason"] = f"Aucune date de fin et publication ancienne ({posted.isoformat()}) : considéré terminé."
        result["_hard_inactive"] = True
        return result

    result["is_active"] = True
    result["reason"] = "Concours actif" + (" — " + ", ".join(details) if details else " (aucune date de fin indiquée)") + "."
    if conditions:
        result["reason"] += " À faire : " + " ; ".join(conditions) + "."
    return result


# ---------------------------------------------------------------------------
# Mode optionnel : modèle local via Ollama (gratuit)
# ---------------------------------------------------------------------------
LLM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_giveaway": {"type": "boolean"},
        "is_active": {"type": "boolean"},
        "accounts_to_follow": {"type": "array", "items": {"type": "string"}},
        "friends_count": {"type": "integer"},
        "custom_keyword": {"type": "string"},
        "needs_save": {"type": "boolean"},
        "question_answer": {"type": "string"},
        "unsupported_conditions": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": [
        "is_giveaway", "is_active", "accounts_to_follow", "friends_count", "custom_keyword",
        "needs_save", "question_answer", "unsupported_conditions", "reason",
    ],
}

LLM_SYSTEM = """Tu lis très attentivement la légende (et l'image si elle est fournie) d'une publication Instagram pour savoir si c'est un concours et QUOI FAIRE EXACTEMENT pour y participer.
Réponds uniquement avec l'objet JSON demandé.

- is_giveaway : vrai seulement s'il y a un lot à gagner (pas une annonce de gagnant, pas un achat obligatoire).
- is_active : faux si la date de fin est dépassée à la date du jour ou si les gagnants sont annoncés.
- accounts_to_follow : pseudos Instagram (sans @) à suivre, y compris les comptes de secours / partenaires. Seulement des pseudos écrits dans la légende ou le compte auteur.
- friends_count : nombre d'amis à identifier en commentaire (0 si aucun).
- custom_keyword : mot / phrase / emoji imposé en commentaire, sinon chaîne vide.
- needs_save : vrai s'il faut enregistrer la publication.
- question_answer : si la légende pose une question ou demande de dire quelque chose en commentaire (ex : « dis-moi quel cadeau te fait envie »), rédige une réponse courte, naturelle, enthousiaste, en français, à la première personne (en t'appuyant sur l'image : nom d'un des lots visibles). Sinon chaîne vide. N'inclus PAS les @ d'amis.
- unsupported_conditions : conditions OBLIGATOIRES que seul un humain peut faire (partager en story, envoyer un message privé, s'abonner sur TikTok/YouTube/Facebook, acheter). Ne mets PAS les actions présentées comme bonus ou optionnelles.
- reason : une phrase courte en français résumant ta décision."""


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


async def _llm_analyze(caption, author, post_date, image_b64, cfg) -> dict[str, Any]:
    user = (
        f"Date du jour : {date.today().isoformat()}\n"
        f"Compte auteur : {('@' + author) if author else 'inconnu'}\n"
        f"Date de publication : {post_date or 'inconnue'}\n\nLégende :\n\"\"\"\n{caption[:4000]}\n\"\"\""
    )
    message: dict[str, Any] = {"role": "user", "content": user}
    if image_b64:
        message["images"] = [image_b64]
    payload = {
        "model": cfg.get("ollama_model") or "gemma3:4b",
        "stream": False,
        "format": LLM_SCHEMA,
        "options": {"temperature": 0.2},
        "messages": [{"role": "system", "content": LLM_SYSTEM}, message],
    }
    data = await asyncio.to_thread(_ollama_chat, cfg.get("ollama_url") or "http://localhost:11434", payload, 240)
    return json.loads(data["message"]["content"])


def _merge(rules: dict[str, Any], llm: dict[str, Any], caption: str, author: str | None) -> dict[str, Any]:
    low_caption = caption.lower()
    allowed = {author.lower()} if author else set()
    accounts = []
    for a in llm.get("accounts_to_follow") or []:
        a = str(a).strip().lstrip("@")
        if a and (a.lower() in allowed or a.lower() in low_caption):
            accounts.append(a)
    for a in rules["accounts_to_follow"]:
        if a.lower() not in {x.lower() for x in accounts}:
            accounts.append(a)
    try:
        friends = max(int(llm.get("friends_count") or 0), rules["friends_count"])
    except (TypeError, ValueError):
        friends = rules["friends_count"]
    answer = (llm.get("question_answer") or "").strip() or None
    llm_kw = (llm.get("custom_keyword") or "").strip()
    if llm_kw and (rules["question"] or "").lower().find(llm_kw.lower()) >= 0 and not rules["custom_keyword"]:
        llm_kw = ""  # le « mot-clé » n'est en fait qu'une option de la question (« noire ou rose »)
    unsupported = list(dict.fromkeys(rules["unsupported"] + [str(u) for u in llm.get("unsupported_conditions") or [] if str(u).strip()]))
    out = dict(rules)
    out.update(
        is_giveaway=bool(llm.get("is_giveaway")) or rules["is_giveaway"],
        is_active=(bool(llm.get("is_active")) and not rules["_hard_inactive"]) if llm.get("is_giveaway") else rules["is_active"],
        accounts_to_follow=accounts,
        friends_count=max(0, min(friends, 10)),
        custom_keyword=llm_kw or rules["custom_keyword"],
        needs_save=bool(llm.get("needs_save")) or rules["needs_save"],
        answer=answer or rules["answer"],
        question=rules["question"] or (answer and "question détectée par le modèle"),
        unsupported=unsupported,
    )
    if llm.get("reason"):
        out["reason"] = str(llm["reason"]).strip().rstrip(". ")
        if out["is_active"] and rules["conditions"]:
            out["reason"] += ". À faire : " + " ; ".join(rules["conditions"]) + "."
    if rules["_hard_inactive"]:
        out["reason"] = rules["reason"]
    return out


# ---------------------------------------------------------------------------
# Mode optionnel : API gratuite compatible OpenAI (Gemini, Groq, OpenRouter, Pollinations…)
# ---------------------------------------------------------------------------
JSON_HINT = (
    "\n\nRéponds avec UN SEUL objet JSON ayant exactement ces clés : is_giveaway (bool), is_active (bool), "
    "accounts_to_follow (liste de chaînes), friends_count (entier), custom_keyword (chaîne, \"\" si aucun), "
    "needs_save (bool), question_answer (chaîne, \"\" si aucune), unsupported_conditions (liste de chaînes), "
    "reason (chaîne). Aucun texte hors du JSON."
)
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
                    if e.code == 429 and attempt == 0:
                        delay = min(float(e.headers.get("Retry-After") or 15), 30)
                        bus.log(f"API : limite atteinte (429), nouvel essai dans {delay:.0f}s.", "warning")
                        await asyncio.sleep(delay)
                        continue
                    if e.code == 400 and attempt <= 1:
                        # le modèle refuse peut-être l'image ou response_format : on simplifie
                        payload = json.loads(json.dumps(payload))
                        payload.pop("response_format", None)
                        for m in payload["messages"]:
                            if isinstance(m.get("content"), list):
                                m["content"] = "\n".join(c.get("text", "") for c in m["content"] if c.get("type") == "text")
                        bus.log("API : requête refusée (400), nouvel essai sans image ni mode JSON.", "debug")
                        continue
                    raise
            raise AnalyzerError("API : échec après plusieurs essais.")
        finally:
            _api_last = asyncio.get_running_loop().time()


async def _api_analyze(caption, author, post_date, image_b64, cfg) -> dict[str, Any]:
    text = (
        f"Date du jour : {date.today().isoformat()}\n"
        f"Compte auteur : {('@' + author) if author else 'inconnu'}\n"
        f"Date de publication : {post_date or 'inconnue'}\n\nLégende :\n\"\"\"\n{caption[:4000]}\n\"\"\""
    )
    content: Any = text
    if image_b64:
        content = [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image_b64}},
        ]
    payload = {
        "model": cfg.get("api_model") or "gemini-flash-latest",
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": LLM_SYSTEM + JSON_HINT},
            {"role": "user", "content": content},
        ],
    }
    data = await _api_request(cfg, payload)
    return _extract_json(data["choices"][0]["message"]["content"])


async def api_status(cfg: dict[str, Any]) -> dict[str, Any]:
    if not cfg.get("api_base_url"):
        return {"ok": False, "message": "Adresse de l'API manquante."}
    payload = {
        "model": cfg.get("api_model") or "",
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "Réponds juste : OK"}],
    }
    try:
        data = await _api_request(cfg, payload)
        reply = str(data["choices"][0]["message"]["content"]).strip()[:40]
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
) -> dict[str, Any]:
    rules = analyze_sync(caption, author, post_date)
    cfg = cfg or {}
    mode = cfg.get("analyzer_mode")
    if mode not in ("ollama", "api"):
        return rules
    try:
        if mode == "ollama":
            llm = await _llm_analyze(caption, author, post_date, image_b64, cfg)
        else:
            llm = await _api_analyze(caption, author, post_date, image_b64, cfg)
    except urllib.error.HTTPError as e:
        bus.log(f"{'API' if mode == 'api' else 'Ollama'} en erreur ({_http_detail(e)}) : repli sur l'analyse par règles.", "warning")
        return rules
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError, TimeoutError, AnalyzerError) as e:
        bus.log(f"{'API' if mode == 'api' else 'Ollama'} indisponible ({e}) : repli sur l'analyse par règles.", "warning")
        return rules
    return _merge(rules, llm, caption, author)
