"""Classement des concours : échéance la plus proche d'abord + bonus pour PS5 / PC / jeux vidéo."""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from analyzer import _norm

# Mots-clés « gaming / PC » (comparés sans accents ni majuscules, en mots entiers).
DEFAULT_KEYWORDS = [
    "ps5", "ps 5", "ps4", "playstation", "xbox", "nintendo", "switch", "steam", "pc", "pc gamer", "pc gaming",
    "pc portable", "ordinateur", "laptop", "rtx", "carte graphique", "gpu", "console", "manette", "gaming", "gamer",
    "jeu video", "jeux video", "video game", "rog", "razer", "clavier mecanique",
]

# Bonus ajouté au score d'échéance (0-100) quand le concours parle de gaming / PC.
MODES = {
    "off": 0,          # pas de classement : ordre d'arrivée
    "deadline": 25,    # l'échéance prime, le gaming départage
    "balanced": 70,    # un concours gaming à ~10 j passe devant un autre qui finit demain
    "gaming": 200,     # le gaming passe toujours en premier, puis l'échéance
}
MODE_LABELS = {
    "off": "ordre d'arrivée",
    "deadline": "échéance d'abord",
    "balanced": "équilibré",
    "gaming": "gaming / PC d'abord",
}


def urgency(days_left: int | None) -> int:
    """0-100 : plus la fin est proche, plus c'est haut. Sans date de fin : tout en bas."""
    if days_left is None:
        return 12
    if days_left <= 0:
        return 100
    if days_left <= 3:
        return 100 - 8 * days_left          # 1 j → 92, 2 j → 84, 3 j → 76
    if days_left <= 7:
        return 76 - 6 * (days_left - 3)     # 4 j → 70, 7 j → 52
    if days_left <= 14:
        return 52 - 3 * (days_left - 7)     # 8 j → 49, 14 j → 31
    return max(10, 31 - (days_left - 14))


def clean_keywords(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        v = _norm(str(v)).strip()
        if v and v not in out:
            out.append(v)
    return out[:80]


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    norm = _norm(text)
    hits = []
    for kw in clean_keywords(keywords):
        pattern = r"(?<![a-z0-9])" + re.escape(kw).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
        if re.search(pattern, norm):
            hits.append(kw)
    return hits


def days_left(end_date: str | None, today: date | None = None) -> int | None:
    if not end_date:
        return None
    try:
        return (date.fromisoformat(end_date) - (today or date.today())).days
    except ValueError:
        return None


def score_result(result: dict[str, Any], caption: str, cfg: dict[str, Any], today: date | None = None) -> tuple[int, str]:
    """(score, explication lisible). Plus le score est haut, plus le concours passe tôt."""
    mode = cfg.get("priority_mode", "balanced")
    keywords = cfg.get("priority_keywords") or DEFAULT_KEYWORDS
    d = days_left(result.get("end_date"), today)
    urg = urgency(d)
    parts = []
    if d is None:
        parts.append("pas de date de fin")
    elif d <= 0:
        parts.append("finit aujourd'hui")
    else:
        parts.append(f"finit dans {d} j")
    score = urg
    hits = keyword_hits(caption, keywords)
    if hits and MODES.get(mode, 0):
        score += MODES[mode]
        parts.append("gaming/PC : " + ", ".join(hits[:3]))
    return score, " · ".join(parts)
