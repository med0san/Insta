"""Persistance SQLite (config + historique des concours) via aiosqlite."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "giveaways.db"

DEFAULT_CONFIG: dict[str, Any] = {
    "friends_to_tag": [],
    "hashtags": ["concours", "jeuconcours", "giveawayfr"],
    "daily_quota": 0,          # 0 = aucun plafond journalier
    "dry_run": True,
    "headless": True,          # navigateur invisible pendant que le bot tourne
    "posts_per_hashtag": 12,   # nombre max de posts examinés par hashtag et par cycle
    "analyzer_mode": "rules",  # "rules" (gratuit, hors ligne) ou "ollama" (modèle local gratuit)
    "ollama_model": "gemma3:4b",
    "ollama_url": "http://localhost:11434",
    "bot_enabled": False,      # le bot doit tourner : relancé automatiquement si le serveur redémarre
    "block_cooldown_min": 120, # pause (minutes) après un « Action bloquée », puis reprise automatique
    "priority_mode": "balanced",  # off | deadline | balanced | gaming  (voir priority.py)
    "priority_keywords": [],      # vide = liste par défaut PS5 / PC / jeux vidéo
    "do_story": True,          # republier / mettre en story quand le concours le demande
    "force_participate": True, # participer même si une condition est impossible (participation partielle)
    "speed": "human",          # "human" (3-9 s / 45-120 s) ou "fast" (1,5-4 s / 10-25 s)
    "api_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
    "api_model": "gemini-2.5-flash",
    "api_key": "",             # clé gratuite (stockée localement en clair), vide pour les services sans clé
    "default_answer": "Le premier cadeau me fait trop envie 😍",  # réponse aux questions posées en commentaire
    "ig_username": "",
    "ig_password": "",         # stocké en clair, uniquement si l'utilisateur coche "Mémoriser"
}

STATUSES = ("Participé", "Partiel", "Simulé", "Ignoré", "Erreur")


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                account TEXT,
                hashtag TEXT,
                status TEXT NOT NULL,
                reason TEXT,
                comment TEXT,
                created_at TEXT NOT NULL
            )"""
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_history_created ON history(created_at)")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS candidates (
                url TEXT PRIMARY KEY,
                author TEXT,
                hashtag TEXT,
                caption TEXT,
                result TEXT NOT NULL,
                end_date TEXT,
                created_at TEXT NOT NULL
            )"""
        )
        async with db.execute("PRAGMA table_info(history)") as cur:
            cols = {row[1] async for row in cur}
        for col in ("caption", "actions"):          # migration des bases existantes
            if col not in cols:
                await db.execute(f"ALTER TABLE history ADD COLUMN {col} TEXT")
        # migration unique : l'ancien plafond (10) est supprimé, le bot est désormais illimité
        async with db.execute("SELECT 1 FROM config WHERE key = 'quota_removed'") as cur:
            done = await cur.fetchone()
        if not done:
            await db.execute("INSERT OR REPLACE INTO config(key, value) VALUES ('daily_quota', ?)", (json.dumps(0),))
            await db.execute("INSERT OR REPLACE INTO config(key, value) VALUES ('quota_removed', ?)", (json.dumps(True),))
        # migration : l'alias « gemini-flash-latest » était souvent surchargé (503)
        await db.execute("UPDATE config SET value = ? WHERE key = 'api_model' AND value = ?",
                         (json.dumps("gemini-2.5-flash"), json.dumps("gemini-flash-latest")))
        for key, value in DEFAULT_CONFIG.items():
            await db.execute(
                "INSERT OR IGNORE INTO config(key, value) VALUES (?, ?)", (key, json.dumps(value))
            )
        await db.commit()


async def get_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key, value FROM config") as cur:
            async for key, value in cur:
                try:
                    cfg[key] = json.loads(value)
                except json.JSONDecodeError:
                    pass
    return cfg


async def set_config(values: dict[str, Any]) -> dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        for key, value in values.items():
            if key not in DEFAULT_CONFIG:
                continue
            await db.execute(
                "INSERT INTO config(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
        await db.commit()
    return await get_config()


async def add_candidate(url: str, author: str | None, hashtag: str | None, caption: str,
                        result: dict[str, Any], end_date: str | None) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO candidates(url, author, hashtag, caption, result, end_date, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (url, author, hashtag, caption, json.dumps(result, ensure_ascii=False), end_date, now),
        )
        await db.commit()


async def list_candidates() -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM candidates ORDER BY created_at") as cur:
            rows = [dict(r) async for r in cur]
    for r in rows:
        try:
            r["result"] = json.loads(r["result"])
        except (TypeError, ValueError):
            r["result"] = {}
    return rows


async def remove_candidate(url: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM candidates WHERE url = ?", (url,))
        await db.commit()


async def purge_candidates(max_age_days: int = 3) -> None:
    limit = (datetime.now() - timedelta(days=max_age_days)).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM candidates WHERE created_at < ?", (limit,))
        await db.commit()


async def url_known(url: str) -> bool:
    """Déjà traité (historique) ou déjà en file d'attente."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM history WHERE url = ? UNION ALL SELECT 1 FROM candidates WHERE url = ?", (url, url)
        ) as cur:
            return await cur.fetchone() is not None


async def url_exists(url: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM history WHERE url = ?", (url,)) as cur:
            return await cur.fetchone() is not None


async def add_history(
    url: str,
    account: str | None,
    hashtag: str | None,
    status: str,
    reason: str | None,
    comment: str | None = None,
    caption: str | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    actions_json = json.dumps(actions, ensure_ascii=False) if actions is not None else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO history(url, account, hashtag, status, reason, comment, created_at, caption, actions)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET status = excluded.status,
                   reason = excluded.reason, comment = excluded.comment,
                   created_at = excluded.created_at, caption = excluded.caption, actions = excluded.actions""",
            (url, account, hashtag, status, reason, comment, now, caption, actions_json),
        )
        await db.commit()


async def list_history(limit: int = 300) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = [dict(row) async for row in cur]
    for r in rows:
        try:
            r["actions"] = json.loads(r["actions"]) if r.get("actions") else []
        except (TypeError, ValueError):
            r["actions"] = []
    return rows


async def clear_history() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM history")
        await db.commit()


async def count_today(status: str | tuple[str, ...]) -> int:
    statuses = (status,) if isinstance(status, str) else tuple(status)
    today = datetime.now().strftime("%Y-%m-%d")
    marks = ",".join("?" * len(statuses))
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM history WHERE status IN ({marks}) AND substr(created_at, 1, 10) = ?",
            (*statuses, today),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0
