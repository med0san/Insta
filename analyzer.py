"""Analyse des légendes Instagram avec Claude Haiku (réponse JSON stricte)."""
from __future__ import annotations

import json
import os
from datetime import date
from typing import Any

import anthropic

MODEL = "claude-haiku-4-5"

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_giveaway": {"type": "boolean"},
        "is_active": {"type": "boolean"},
        "accounts_to_follow": {"type": "array", "items": {"type": "string"}},
        "friends_count": {"type": "integer"},
        "custom_keyword": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "reason": {"type": "string"},
    },
    "required": [
        "is_giveaway",
        "is_active",
        "accounts_to_follow",
        "friends_count",
        "custom_keyword",
        "reason",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """Tu analyses des légendes de publications Instagram pour détecter les concours (giveaways / jeux-concours).
Réponds UNIQUEMENT avec l'objet JSON demandé, sans texte autour.

Règles :
- is_giveaway : true seulement si la publication organise réellement un concours avec un lot à gagner (pas une simple promo, un code promo, une annonce de gagnant ou un concours payant/achat obligatoire).
- is_active : true si le concours est encore ouvert à la date du jour. Si une date de fin est mentionnée et dépassée, ou si la légende annonce les gagnants / "concours terminé", mets false. Si aucune date n'est indiquée, considère-le actif sauf si la publication est manifestement ancienne (plus de 30 jours).
- accounts_to_follow : liste des pseudos Instagram (sans '@') qu'il faut suivre pour participer. Si la légende demande de suivre "notre compte" / "nous" / "la page", inclus le compte auteur fourni. Liste vide si aucun abonnement requis.
- friends_count : nombre d'amis à identifier en commentaire (0 si non demandé). Si "identifie 2 amis" → 2. Si "identifie tes amis" sans nombre → 1. Maximum raisonnable : 5.
- custom_keyword : mot, phrase ou emoji précis à écrire en commentaire si la légende en exige un (ex : "commente PARTICIPE", "réponds à la question"), sinon null. Si une question est posée, donne une réponse courte et plausible.
- reason : une phrase courte en français expliquant ta décision (lot, conditions, date de fin)."""


class AnalyzerError(RuntimeError):
    pass


_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise AnalyzerError(
                "ANTHROPIC_API_KEY n'est pas défini dans l'environnement du serveur."
            )
        _client = anthropic.AsyncAnthropic()
    return _client


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    accounts = []
    for acc in data.get("accounts_to_follow") or []:
        acc = str(acc).strip().lstrip("@").strip("/")
        if acc and acc not in accounts:
            accounts.append(acc)
    try:
        friends = int(data.get("friends_count") or 0)
    except (TypeError, ValueError):
        friends = 0
    keyword = data.get("custom_keyword")
    keyword = str(keyword).strip() if keyword else None
    return {
        "is_giveaway": bool(data.get("is_giveaway")),
        "is_active": bool(data.get("is_active")),
        "accounts_to_follow": accounts,
        "friends_count": max(0, min(friends, 10)),
        "custom_keyword": keyword or None,
        "reason": str(data.get("reason") or "").strip() or "Aucune raison fournie.",
    }


async def analyze_caption(
    caption: str,
    author: str | None = None,
    post_date: str | None = None,
) -> dict[str, Any]:
    """Envoie la légende à Claude Haiku et renvoie le JSON normalisé."""
    client = _get_client()
    user_content = (
        f"Date du jour : {date.today().isoformat()}\n"
        f"Compte auteur de la publication : {('@' + author) if author else 'inconnu'}\n"
        f"Date de publication : {post_date or 'inconnue'}\n\n"
        f"Légende :\n\"\"\"\n{caption[:4000]}\n\"\"\""
    )
    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            output_config={"format": {"type": "json_schema", "schema": RESULT_SCHEMA}},
        )
    except anthropic.AuthenticationError as e:
        raise AnalyzerError(f"Clé API Anthropic invalide : {e.message}") from e
    except anthropic.RateLimitError as e:
        raise AnalyzerError(f"Limite de débit Anthropic atteinte : {e.message}") from e
    except anthropic.APIStatusError as e:
        raise AnalyzerError(f"Erreur API Anthropic ({e.status_code}) : {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise AnalyzerError(f"Connexion à l'API Anthropic impossible : {e}") from e

    if response.stop_reason == "refusal":
        raise AnalyzerError("Le modèle a refusé d'analyser cette légende.")

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise AnalyzerError(f"Réponse JSON invalide : {text[:200]}") from e
    return _normalize(data)
