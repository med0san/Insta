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
| **Google Gemini** (`gemini-2.5-flash`) | gratuite, sans carte : [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | oui | meilleur choix |
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

## Vérifier ce que fait le bot

- **Tester une légende** : collez la légende d'un concours, le bot affiche la liste exacte des actions qu'il
  ferait (commentaire compris) sans rien publier. Idéal pour vérifier un concours avant de lancer le bot.
- **Historique détaillé** : cliquez sur une ligne pour voir la légende lue par le bot et chaque action
  avec son résultat (✓ réussi, ✗ échec et pourquoi, ○ simulé). Statut **Partiel** = au moins une action a échoué.
- En mode réel, chaque action est indépendante : un bouton introuvable n'empêche pas les suivantes.

Garde-fous contre les erreurs des modèles d'IA (surtout les petits modèles locaux) :
- une phrase d'accroche (« Envie de gagner … ? », « Prêt à … ? ») n'est jamais traitée comme une question ;
- la réponse à une vraie question est rédigée par un 2e appel dédié, et rejetée si elle recopie la légende ;
- un mot imposé n'est retenu que s'il suit une consigne (« commente X », « commente avec 🎄 », guillemets) ;
- story, enregistrement et amis à identifier ne sont acceptés que si la légende en parle ;
- les comptes **identifiés sur la photo** et les **co-auteurs** du post sont relevés sur la page.

## Réglages « ne rien ignorer »

| Réglage | Effet |
|---|---|
| **Republier / mettre en story** (défaut : ON) | Si la légende parle de story / republication (obligatoire ou bonus), le bot l'ajoute à votre story si Instagram web propose l'option, sinon clique sur le bouton **republication** (↻). |
| **Participer quand même** (défaut : ON) | Une condition impossible (message privé, abonnement TikTok/Facebook…) ou un manque d'amis n'annule plus la participation : le bot fait tout le reste (follow, like, commentaire avec les amis disponibles…). |
| **Vitesse** | *Humaine* (3-9 s entre actions, 45-120 s entre concours) ou *Rapide* (1,5-4 s / 10-25 s). |
| **Quota journalier** | aucun plafond par défaut. |

Les dates de fin et les annonces de gagnants sont jugées par des règles fixes (un petit modèle local se trompe
souvent sur « le concours est-il encore actif ? »). Un concours réellement expiré reste donc ignoré, avec
l'extrait de légende qui le prouve.

Gardé volontairement : l'**arrêt immédiat** si Instagram affiche « Action bloquée » / « Try Again Later ».
Continuer après un blocage fait passer le compte en restriction, voire en suspension.

## Priorité des concours

Le bot ne participe plus dans l'ordre où il tombe sur les posts : il **lit d'abord tout ce qu'il trouve**, met les
concours actifs en file d'attente, les **classe**, puis participe du plus au moins prioritaire. Aucun concours n'est
écarté pour autant : les moins prioritaires passent en dernier.

- **Échéance** : plus la fin est proche, plus le score est haut (finit aujourd'hui 100, demain 92, 3 j 76, 7 j 52,
  14 j 31 ; sans date de fin : tout en bas). Un concours expiré pendant l'attente est écarté (noté dans l'historique).
- **Gaming / PC** : bonus si la légende parle de PS5, PlayStation, Xbox, Nintendo, Switch, PC, PC gamer, RTX, carte
  graphique, console, manette, jeux vidéo… (liste modifiable dans l'interface, un mot par virgule).
- **Réglage « Priorité des concours »** : *Équilibré* (défaut : un concours gaming qui finit dans ~10 jours passe devant
  un autre qui finit demain), *Échéance d'abord*, *Gaming / PC d'abord*, ou *ordre d'arrivée*.
- Le classement et le score de chaque concours sont affichés dans les logs et dans « Tester une légende ».

## Fiabilité des actions

Chaque action est **vérifiée** avant d'être annoncée comme réussie, sinon elle apparaît en ✗ avec la raison :
like (l'icône du *post* doit passer à « Unlike », pas celle d'un commentaire), enregistrer, suivre (le bouton doit
passer à « Following »), commentaire (la zone de saisie doit se vider), republication (icône, compteur +1 ou message
de confirmation). Instagram web ne propose pas « Ajouter à ma story » : le bot utilise le bouton ↻ republier.

## Mode autonome (toute la nuit)

Le bot tourne en boucle sans surveillance :

- **Aucun plafond** par défaut (réglage « Plafond journalier » : 0 = illimité ; une valeur > 0 met le bot en pause
  jusqu'au lendemain une fois atteinte, sans l'arrêter).
- **Les erreurs ne l'arrêtent pas** : un post cassé est noté « Erreur » et le suivant est traité ; si le navigateur
  plante ou refuse de démarrer, il est relancé après une attente croissante. Chaque cycle utilise un Chromium neuf.
  Arrêt seulement après 8 cycles consécutifs en échec.
- **Pas de veille du PC** tant que le bot tourne (Windows, macOS).
- **Après « Action bloquée » / « Try Again Later »** : pause (120 min par défaut, réglable) puis reprise automatique.
  **Au 3<sup>e</sup> blocage en 24 h le bot s'arrête** pour ne pas aggraver la restriction du compte.
- **Reprise après plantage ou redémarrage du serveur** : `run.bat` / `run.sh` relancent le serveur, qui relance le bot
  s'il tournait. Le bouton « Arrêter le bot » désactive cette reprise.
- Entre deux scans : 3-8 min si le dernier scan a trouvé du nouveau, 15-30 min sinon. Ajoutez des hashtags pour plus de volume.
- Session Instagram expirée : le bot s'arrête (il ne peut pas se reconnecter seul si Instagram demande un code).

Laissez la fenêtre `run.bat` ouverte et l'ordinateur branché sur secteur. Désactivez le Dry-Run pour participer pour de vrai.

## Passer en mode réel

Le **Dry-Run** est activé par défaut. Désactivez-le avec l'interrupteur (une confirmation est demandée) pour
que le bot like, enregistre, suive et commente réellement. Commencez avec un quota bas.

## Limites

Les règles reconnaissent les formulations courantes mais peuvent rater une tournure inhabituelle.
Les sélecteurs Instagram peuvent changer. Instagram web ne propose pas toujours « Ajouter à ma story » :
dans ce cas le bot republie le post à la place. Les envois en message privé et les abonnements hors Instagram
ne sont pas automatisés. Les règles se trouvent dans `analyzer.py`.
