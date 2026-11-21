"""Persistance SQLite (config + historique des concours) via aiosqlite."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "giveaways.db"

DEFAULT_CONFIG: dict[str, Any] = {
    "friends_to_tag": [],
    "hashtags": ["concours", "jeuconcours", "giveawayfr"],
    "daily_quota": 10,
    "dry_run": True,
    "headless": True,          # navigateur invisible pendant que le bot tourne
    "posts_per_hashtag": 12,   # nombre max de posts examinés par hashtag et par cycle
    "analyzer_mode": "rules",  # "rules" (gratuit, hors ligne) ou "ollama" (modèle local gratuit)
    "ollama_model": "gemma3:4b",
    "ollama_url": "http://localhost:11434",
    "api_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
    "api_model": "gemini-2.5-flash",
    "api_key": "",             # clé gratuite (stockée localement en clair), vide pour les services sans clé
    "default_answer": "Le premier cadeau me fait trop envie 😍",  # réponse aux questions posées en commentaire
    "ig_username": "",
    "ig_password": "",         # stocké en clair, uniquement si l'utilisateur coche "Mémoriser"
}

STATUSES = ("Participé", "Simulé", "Ignoré", "Erreur")


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
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO history(url, account, hashtag, status, reason, comment, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET status = excluded.status,
                   reason = excluded.reason, comment = excluded.comment,
                   created_at = excluded.created_at""",
            (url, account, hashtag, status, reason, comment, now),
        )
        await db.commit()


async def list_history(limit: int = 300) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(row) async for row in cur]


async def clear_history() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM history")
        await db.commit()


async def count_today(status: str) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM history WHERE status = ? AND substr(created_at, 1, 10) = ?",
            (status, today),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0
