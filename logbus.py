"""Bus de logs en mémoire diffusé aux clients WebSocket."""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from typing import Any

LEVELS = ("info", "success", "warning", "error", "ai", "action", "debug")


class LogBus:
    def __init__(self, maxlen: int = 600) -> None:
        self.history: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.subscribers: set[asyncio.Queue] = set()

    def _publish(self, event: dict[str, Any]) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def log(self, message: str, level: str = "info") -> None:
        entry = {
            "type": "log",
            "ts": datetime.now().strftime("%H:%M:%S"),
            "level": level if level in LEVELS else "info",
            "message": message,
        }
        self.history.append(entry)
        print(f"[{entry['ts']}] {entry['level'].upper():7} {message}", flush=True)
        self._publish(entry)

    def notify(self, kind: str) -> None:
        """Signale au frontend qu'il doit rafraîchir une partie de l'UI (status/history/config)."""
        self._publish({"type": "refresh", "what": kind})

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)


bus = LogBus()
