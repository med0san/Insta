"""Accès depuis un autre appareil (téléphone) : mot de passe obligatoire dès que le serveur écoute sur le réseau.

- BOT_HOST=127.0.0.1 (défaut) : serveur local, aucune authentification.
- BOT_HOST=0.0.0.0 : serveur joignable depuis le réseau → ACCESS_PASSWORD est OBLIGATOIRE (sinon le serveur refuse de démarrer).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import socket
import time

HOST = os.environ.get("BOT_HOST", "127.0.0.1")
PASSWORD = os.environ.get("ACCESS_PASSWORD", "")
COOKIE = "gb_session"
REMOTE = HOST not in ("127.0.0.1", "localhost", "::1")
MAX_FAILURES = 8          # tentatives de mot de passe ratées par IP…
WINDOW = 15 * 60          # …sur cette durée (secondes) avant blocage temporaire

if REMOTE and not PASSWORD:
    raise SystemExit(
        "REFUS DE DÉMARRER : le serveur écoute sur le réseau (BOT_HOST=%s) sans mot de passe.\n"
        "Définissez ACCESS_PASSWORD (utilisez run-mobile.bat)." % HOST
    )

_failures: dict[str, list[float]] = {}


def enabled() -> bool:
    return bool(PASSWORD)


def _token() -> str:
    return hmac.new(PASSWORD.encode(), b"giveaway-bot-session-v1", hashlib.sha256).hexdigest()


def is_authed(cookie_value: str | None) -> bool:
    return bool(cookie_value) and hmac.compare_digest(cookie_value, _token())


def check_password(candidate: str, ip: str) -> str | None:
    """Renvoie le jeton de session si le mot de passe est bon, None sinon (avec limitation des essais)."""
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < WINDOW]
    if len(recent) >= MAX_FAILURES:
        _failures[ip] = recent
        return None
    if hmac.compare_digest(candidate.encode(), PASSWORD.encode()):
        _failures.pop(ip, None)
        return _token()
    _failures[ip] = recent + [now]
    return None


def locked(ip: str) -> bool:
    now = time.time()
    return len([t for t in _failures.get(ip, []) if now - t < WINDOW]) >= MAX_FAILURES


def lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))      # aucun paquet envoyé : sert à trouver l'interface réseau
            return s.getsockname()[0]
    except OSError:
        return None


LOGIN_HTML = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Giveaway Bot</title>
<style>
 :root{color-scheme:dark} body{margin:0;background:#0a0a0a;color:#e5e5e5;font:16px system-ui,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center}
 form{background:#141414;border:1px solid #262626;border-radius:14px;padding:24px;width:min(340px,90vw)}
 h1{font-size:17px;margin:0 0 4px} p{color:#8a8a8a;font-size:13px;margin:0 0 16px}
 input,button{width:100%;box-sizing:border-box;border-radius:9px;font-size:16px;padding:12px;border:1px solid #262626}
 input{background:#0a0a0a;color:#e5e5e5;margin-bottom:12px} button{background:#fafafa;color:#0a0a0a;font-weight:600;border:0}
 #e{color:#f87171;font-size:13px;min-height:18px;margin-top:10px}
</style></head><body>
<form onsubmit="go(event)"><h1>Giveaway Bot</h1><p>Accès protégé</p>
<input id="p" type="password" placeholder="Mot de passe" autocomplete="current-password" autofocus>
<button>Se connecter</button><div id="e"></div></form>
<script>
async function go(ev){ev.preventDefault();const e=document.getElementById('e');e.textContent='';
 try{const r=await fetch('/access/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('p').value})});
 if(r.ok){location.href='/';}else{const d=await r.json().catch(()=>({}));e.textContent=d.detail||'Mot de passe incorrect';}}
 catch(x){e.textContent='Serveur injoignable';}}
</script></body></html>"""
