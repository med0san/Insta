"""Empêche la mise en veille du PC pendant que le bot tourne (Windows / macOS)."""
from __future__ import annotations

import subprocess
import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

_proc: subprocess.Popen | None = None
_active = False


def enable() -> bool:
    """Active l'anti-veille. Doit être appelé depuis le thread de la boucle asyncio. Renvoie True si actif."""
    global _proc, _active
    try:
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            _active = True
        elif sys.platform == "darwin":
            _proc = subprocess.Popen(["caffeinate", "-i"])  # tant que ce processus vit, le Mac ne dort pas
            _active = True
    except Exception:  # noqa: BLE001
        _active = False
    return _active


def disable() -> None:
    global _proc, _active
    try:
        if sys.platform == "win32" and _active:
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        if _proc is not None:
            _proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    _proc, _active = None, False
