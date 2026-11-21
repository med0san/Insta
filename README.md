# Giveaway Bot — participation automatique aux concours Instagram

Application web locale (FastAPI + Playwright + SQLite) pour scanner des hashtags, détecter les concours
actifs et y participer (like, follow, commentaire avec identification d'amis).

**100 % gratuit : aucune clé API, aucun crédit.** L'analyse des légendes est faite en local par des règles
(mots-clés FR/EN, dates de fin, @mentions, questions…), sans service externe ; un modèle local Ollama est optionnel.

> ⚠️ L'automatisation d'un compte va à l'encontre des conditions d'utilisation d'Instagram et peut entraîner
> des restrictions ou une suspension du compte. Le mode **Dry-Run** est activé par défaut : commencez par là.

## Lancement

```bash
./run.sh        # Windows : run.bat
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
| `bot.py` | Boucle asyncio : scan des hashtags → analyse → participation ou simulation |
| `analyzer.py` | Analyse locale par règles → `{is_giveaway, is_active, accounts_to_follow, friends_count, custom_keyword, reason}` |
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

## Lecture des conditions

Pour chaque post, le bot lit **toute la légende** et en déduit la liste exacte de ce qu'il faut faire
(visible dans les logs, ligne « • … », et dans la colonne *Raison* de l'historique) :

- comptes à suivre (y compris « compte de secours » / partenaires), liker, **enregistrer** le post ;
- nombre d'amis à identifier (tirés au hasard dans votre liste) ;
- mot-clé imposé (« commente le mot « SOLEIL » ») ;
- **question à laquelle répondre** (« dis-moi quel cadeau te fait envie ») : la réponse est ajoutée au commentaire ;
- date de fin / gagnants déjà annoncés → concours ignoré ;
- conditions **impossibles pour le bot** (partage en story obligatoire, message privé, abonnement TikTok/YouTube,
  achat) → concours ignoré avec la raison. Les actions présentées comme *bonus* sont ignorées sans bloquer.

Trois modes (réglage « Lecture des conditions ») :

| Mode | Coût | Plus |
|---|---|---|
| **Règles intégrées** (défaut) | gratuit, hors ligne | rapide ; les questions reçoivent la *réponse par défaut* configurée |
| **API IA gratuite** (recommandé) | gratuit, rien à installer | une IA lit la légende **et l'image**, comprend les conditions et rédige la réponse aux questions |
| **Ollama** | gratuit, local | comme l'API, mais sur votre PC (modèle à télécharger) |

**API IA gratuite** — n'importe quel service compatible OpenAI. Préréglages dans l'interface :

| Service | Clé | Image | Remarque |
|---|---|---|---|
| **Google Gemini** | gratuite, sans carte : [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | oui | meilleur choix |
| Groq | gratuite : [console.groq.com/keys](https://console.groq.com/keys) | non (texte) | très rapide |
| OpenRouter | gratuite : [openrouter.ai/keys](https://openrouter.ai/keys) | oui (modèles `:free`) | |
| Pollinations | **aucune** | non | service public, qualité et disponibilité variables |

Collez la clé, cliquez **Tester**. Les noms de modèles gratuits changent : modifiez le champ « modèle » si besoin.
Les offres gratuites limitent les requêtes par minute : le bot espace les appels (≥ 4,5 s), réessaie une fois sur
une limite (429) et **retombe sur les règles** si le service est en panne ou refuse. Seules la légende, le
pseudo de l'auteur et l'image du post (publics) sont envoyés — jamais vos identifiants ni vos amis.
La clé est stockée en clair dans `giveaways.db` (local) et n'est jamais renvoyée par l'API du serveur.

**Ollama** : installez [Ollama](https://ollama.com), lancez `ollama pull gemma3:4b`, choisissez
« Modèle local Ollama » et cliquez **Tester**.

Dans les deux modes IA, les pseudos inventés par le modèle sont filtrés (seuls ceux présents dans la légende sont gardés).

## Passer en mode réel

Le **Dry-Run** est activé par défaut. Désactivez-le avec l'interrupteur (une confirmation est demandée) pour
que le bot like, enregistre, suive et commente réellement. Commencez avec un quota bas.

## Limites

Les règles reconnaissent les formulations courantes mais peuvent rater une tournure inhabituelle ; en cas de
doute le concours est ignoré plutôt que traité à tort. Les sélecteurs Instagram peuvent changer.
Les règles se trouvent dans `analyzer.py`.
