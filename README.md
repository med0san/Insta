# Giveaway Bot — participation automatique aux concours Instagram

Application web locale (FastAPI + Playwright + Claude Haiku + SQLite) pour scanner des hashtags,
détecter les concours actifs avec l'IA et y participer (like, follow, commentaire avec identification d'amis).

> ⚠️ L'automatisation d'un compte va à l'encontre des conditions d'utilisation d'Instagram et peut entraîner
> des restrictions ou une suspension du compte. Le mode **Dry-Run** est activé par défaut : commencez par là.

## Lancement

```bash
export ANTHROPIC_API_KEY=sk-ant-...     # Windows : set ANTHROPIC_API_KEY=...
./run.sh                                # Windows : run.bat
```

Puis ouvrez **http://localhost:8000**.

`run.sh` / `run.bat` créent le `.venv`, installent les dépendances et Chromium au premier lancement.
Installation manuelle équivalente :

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
```

## Utilisation

1. **Connexion Instagram**
   - *Identifiants* : identifiant + mot de passe, Playwright se connecte. Si Instagram demande un code 2FA,
     un champ apparaît pour le saisir. Le mot de passe n'est enregistré (en clair, dans `giveaways.db`)
     que si « Mémoriser » est coché.
   - *Manuelle (Chromium)* : ouvre une fenêtre Chromium visible, vous vous connectez à la main (le plus fiable
     contre les captchas). La session est détectée automatiquement.
   - Dans les deux cas, les cookies sont conservés dans `./ig_session`.
2. **Configuration** (sauvegardée automatiquement en SQLite, prise en compte en direct par le bot) :
   amis à identifier, hashtags, quota journalier, posts examinés par hashtag, Dry-Run, navigateur invisible.
3. **Démarrer le bot** : suivez les logs en temps réel et l'historique.

## Fonctionnement

| Fichier | Rôle |
|---|---|
| `main.py` | Serveur FastAPI : API REST, WebSocket `/ws/logs`, frontend |
| `bot.py` | Boucle asyncio : scan des hashtags → analyse IA → participation ou simulation |
| `analyzer.py` | Appel à `claude-haiku-4-5` avec sortie JSON stricte (schéma imposé) |
| `instagram.py` | Navigateur Playwright persistant + actions (lecture, like, follow, commentaire) |
| `auth.py` | Connexion par identifiants/2FA ou manuelle |
| `db.py` | SQLite (`giveaways.db`) : config + historique |
| `static/index.html` | Interface (Tailwind + JS vanilla) |

- Les URLs déjà présentes dans l'historique ne sont jamais retraitées.
- Le quota compte les participations du jour **du mode courant** (simulations en Dry-Run, participations réelles sinon).
  Le bot s'arrête quand il est atteint.
- Anti-détection : 3–9 s entre actions, 45–120 s entre concours, 15–30 min entre deux cycles de scan,
  frappe caractère par caractère, et **arrêt immédiat** si Instagram affiche « Action bloquée » / « Try Again Later ».
- Si un concours demande plus d'amis que ceux configurés, il est ignoré.
- Les sélecteurs Instagram (boutons, champ de commentaire) peuvent changer : en cas d'erreur, désactivez
  « Navigateur invisible » pour observer ce que fait Chromium.
