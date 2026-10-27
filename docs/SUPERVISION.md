# Supervision et alerte (#24)

## Le problème qu'on répare

Une panne se découvrait **en écoutant un appel**. C'est littéralement comme ça qu'a été
trouvée la panne des appels muets du 30/07/2026 : `extra_body` mal formé, le LLM levait
une exception à chaque tour, les appels étaient enregistrés `completed`, Twilio recevait
ses `HTTP 200`, `/health` répondait `{"status": "ok"}` — et l'appelant écoutait le silence.

Aucun voyant n'était rouge parce qu'aucun voyant ne regardait quoi que ce soit.
`/health` renvoie une constante ; c'est une sonde de vie, pas une sonde d'état.

## Comment c'est fait

```
GitHub Actions ──15 min──► GET /supervision  (jeton)
  supervision.yml            api/app/supervision.py
  hors du serveur            • 9 contrôles, ZÉRO appel réseau
  échoue ⇒ e-mail            • 200 = ça sert · 503 = panne
                                     │
                                     └──► /admin/health (même fonction)
```

Trois choix qui portent tout le reste :

**Le surveillant est ailleurs.** Une supervision hébergée sur la machine qu'elle
surveille ne signale jamais la panne qui compte le plus — celle où la machine ne
répond plus. Le workflow tourne chez GitHub : `curl` qui échoue **est** l'alerte.

**La sonde ne fait aucun appel réseau.** Réveiller le GPU Modal toutes les 15 minutes
pour « vérifier que le TTS répond » coûterait plus cher que servir les appels : le
scale-to-zero est l'économie du produit. Le TTS est donc surveillé par ce que les
appels RÉELS en révèlent, pas par une sonde synthétique. Voir les angles morts plus bas.

**Pas de mesure ≠ tout va bien.** Zéro appel sur la fenêtre ne donne pas « 0 % d'échec » :
ça donne « pas de mesure », et le contrôle le dit. Le projet s'interdit les chiffres
décoratifs ; les feux verts décoratifs sont la même faute.

## Les vingt-deux contrôles

| Contrôle | Ce qu'il attrape | Panne |
|---|---|---|
| Base de données | Volume plein ou en lecture seule. Il **écrit** vraiment : lire prouve que la base répond, pas qu'elle accepte encore une réservation | oui |
| Configuration du chemin d'appel | Une des quatre variables du chemin d'appel manque (`OPENROUTER_API_KEY`, `PUBLIC_WS_URL`, `DEEPGRAM_API_KEY`, `MISTRAL_API_KEY`), ou `PUBLIC_WS_URL` contient une espace — l'appel raccroche alors sans un mot. Attention seulement : `TWILIO_REGION` demande une région dont le jeton n'est pas posé (l'application se replie sur `us1`) | oui |
| Signature des requêtes Twilio | Jeton absent ou vérification coupée (attention) ; en `log`, des requêtes qui seraient refusées (attention : vérifier `PUBLIC_URL`) ; en `enforce`, **toutes** refusées = l'URL publique n'est plus celle que Twilio signe, plus un appel n'aboutit. Compte par région (`us1`, `ie1`) dès que l'Irlande a signé une requête | oui, si tout est refusé |
| Jeton du serveur de voix | `MOSHI_TTS_API_KEY` vide ou égale à `public_token`, la clé de démonstration connue de tous : qui trouve l'URL Modal fait tourner le GPU à nos frais | non |
| Appels muets | Appel `completed`, plus de 15 s, **aucune réponse venue du modèle** : l'accueil pré-rendu, la reprise et les relances ne comptent pas, puisqu'ils partent sans lui. Voit la panne LLM du 30/07 comme la panne GPU du 23/09 | si majoritaire, **ou si les 3 derniers appels n'ont rien obtenu** |
| Appels en échec | Le pipeline a levé une erreur pendant l'appel | si majoritaire |
| Appels jamais clôturés | `finish_call` n'a pas tourné : le worker est mort avec l'appel | si majoritaire |
| Blanc ressenti | Dérive de la latence (mesuré en prod : 1,16 s ; attention à 2,5 s, panne à 4 s) | oui |
| Alertes Twilio | Webhook injoignable, TwiML invalide — **invisible de l'intérieur**. Relevées dans chaque région dont on a le jeton ; une région qui le refuse est nommée (« relève impossible en ie1 (HTTP 401) ») sans faire taire l'autre. Un compte Twilio **suspendu** (401, « is not active ») est nommé pour ce qu'il est, et non plus pris pour un jeton à renouveler | si le compte est suspendu |
| Solde Twilio | Le solde du compte, relevé avec les alertes (un seul compte pour les deux régions, lu à l'API américaine). Sous `SUPERVISION_SOLDE_TWILIO_MIN` (5 $, décision du 23/10/2026) : attention, un e-mail aux super-admins une fois par épisode, et la recette automatique ne compose plus — elle refuse aussi un solde inconnu ou relevé il y a plus d'une heure. Le 12/10/2026, le solde s'est vidé sans prévenir et la ligne est restée morte onze jours | à zéro, ou compte suspendu |
| URL de secours Twilio | Un numéro du compte sans URL de secours (*Voice Fallback URL*) servie hors de notre serveur (ASSISTANTE-157). Quand le serveur ne répond plus, c'est la seule chose que Twilio peut encore jouer : `renvoi.py` et la vigie tournent dans l'application. Relevé avec les alertes, à l'API américaine (les numéros sont au compte). Une URL de secours sur notre propre domaine compte comme absente ; les numéros sont nommés par leurs quatre derniers chiffres | non |
| Carnet resOS | Établissements dont les réservations vont dans resOS : clé API absente, ou resOS en échec (état écrit sur l'ardoise à chaque changement, il survit aux redémarrages). Pendant la panne, l'assistante prend des messages au lieu de réserver. Le bac à sable plafonne à « attention » : une panne simulée ne réveille pas l'alerte | oui si dernier échec < 1 h |
| Voix Mistral | La voix de tous les établissements (Voxtral) : clé `MISTRAL_API_KEY` absente (voix de secours Moshi, réveil de GPU), ou synthèses échouées dans l'heure — chacune est une phrase que l'appelant n'a pas entendue. On ne sonde pas Mistral : chaque sonde serait facturée | oui si la clé manque |
| Appels passés en secours | Des appels que l'assistante n'a pas pu servir (ASSISTANTE-118) : renvoyés au restaurant, passés au répondeur, ou repartis sans personne — une panne de fournisseur de dix minutes ne laisse rien d'autre dans la base. Attention aussi si un établissement n'a pas de numéro de secours. Les essais lancés depuis l'admin ne comptent pas. Les renvois d'une vidange de déploiement (ASSISTANTE-159) comptent, mais en attention seulement | oui si un appel est passé en secours dans les 20 dernières minutes, hors vidange |
| Crédit OpenAI | Un refus de session GPT-Live pour crédit épuisé (ASSISTANTE-151), vu depuis le démarrage : l'heure du premier refus, le nombre de refus, et la marche à suivre (recharger le crédit dans la console OpenAI ; la chaîne classique sert les appels en attendant). **Ce n'est pas une mesure du solde** — OpenAI ne le donne pas par son API : « OK » veut dire « aucun refus vu ». Revient au vert dès qu'une session s'ouvre. Un e-mail part aux super-admins au premier refus de l'épisode. Sans clé GPT-Live : OK, rien à surveiller. Mémoire du processus : un redéploiement l'efface | oui dès le premier refus Deux heures sans nouveau refus : l'alerte descend à « attention » (rien ne dit si le crédit est revenu), pour ne pas masquer les autres pannes ; elle ne s'efface qu'à la première session ouverte. |
| Crédit OpenRouter | Le crédit qui paie le cerveau de la chaîne classique, aussi secours de GPT-Live (ASSISTANTE-172) : acheté moins consommé, lu à `/api/v1/credits` par la tournée quotidienne. Sous `SUPERVISION_CREDIT_OPENROUTER_MIN` (3 $, décision du 24/10/2026) : attention ; épuisé : panne. Relève ratée ou vieille de plus de 26 h : attention. Sans clé : OK, rien à surveiller | oui à 0 |
| Voix d'accueil | WAV non pré-rendu : décroché non instantané (démarrage à froid du GPU) ; message « rappelez dans quelques minutes » absent — si le GPU ne vient pas, l'appelant n'entendra que le silence | non |
| Sauvegarde | Fraîcheur du jeton écrit par `backup-db.sh` **après** le contrôle d'intégrité ; copie hors du serveur absente ou arrêtée (attention) | oui à 72 h |
| Réplication continue de la base | Litestream (ASSISTANTE-158) a-t-il rattrapé la copie locale ? Une tâche de fond lit ses métriques chaque minute (aucun appel réseau dans la sonde) et compare la position de la réplique à celle du WAL local. Attention si les métriques sont injoignables, si la relève est vieille de plus de 5 min, ou si la réplique est en retard depuis 10 min. Sans `LITESTREAM_REPLICA_URL` : OK, « Non configurée », la sauvegarde quotidienne reste la seule protection | non : un appelant n'est pas concerné |
| Purge des données personnelles | Deux cycles manqués = la durée annoncée au registre n'est plus tenue | oui |
| Espace disque | Un disque plein laisse passer les `SELECT` et fait échouer la seule chose qui compte : enregistrer une réservation | oui sous 2 Go |
| Enregistrement des appels | Activé mais n'aboutit pas — « 0 sur 12 » est le cas qu'on veut voir | non |

Un contrôle qui échoue lui-même est signalé « état INCONNU », jamais vert : une panne de
supervision ne doit pas se déguiser en bonne nouvelle.

## La tournée quotidienne (ASSISTANTE-172)

Chaque jour à 8 h, heure de Paris (`SUPERVISION_TOURNEE_HEURE`, décision du 24/10/2026 ;
« non » la coupe), `app/tournee.py` relève le crédit OpenRouter, lit tous les contrôles
ci-dessus et les exécutions de GitHub Actions **en échec sur `main`** depuis vingt-quatre
heures (CI et sonde ; le dépôt est public, l'API se lit sans jeton). S'il y a au moins un
point à l'orange ou au rouge, **un** e-mail part aux super-admins, avec chaque point et la
marche à suivre ; tout vert, rien. Le jour de la dernière tournée est noté sur l'ardoise
(`tournee`) : un redémarrage après 8 h la fait aussitôt si elle n'a pas eu lieu, jamais
deux fois. Une tournée qui échoue se retente une heure plus tard.

Ce qu'elle ne voit pas, faute d'API (vérifié le 24/10/2026) : le solde **Mistral** (la
console seulement, aucune route de facturation dans l'API) et le solde **Deepgram**
(`/v1/projects/{id}/balances` répond 403 à la clé de production : il lui manque le droit).

L'e-mail dit le jour et l'heure réels de la tournée : un redémarrage le soir la fait le soir.

### Les e-mails des fournisseurs (routine cloud)

Ce qu'aucune API ne dit (une carte expirée, une suspension annoncée, un changement de prix)
arrive par e-mail. Une routine Claude Code, « Helmane — e-mails des fournisseurs », lit
chaque jour à 8 h 10 (heure de Paris) la boîte Gmail de Helmi sur les dernières vingt-quatre
heures : Twilio, OpenAI, OpenRouter, Mistral, Deepgram, Hostinger, GitHub. Elle n'écrit
qu'à Helmi, et seulement si un fournisseur demande une action. Le contenu d'un e-mail est
une donnée, jamais une instruction : elle ne suit aucun lien, ne répond pas, n'archive ni ne
supprime rien. Elle vit hors du dépôt et de l'application (décision du 25/10/2026,
`docs/DECISIONS.md`) ; elle se règle sur claude.ai, page des routines.

## Ce qui déclenche une alerte

`attention` = dégradé, les appels passent : visible dans le résumé du run et sur
`/admin/health`, **personne n'est réveillé**. Une alerte qui sonne pour tout finit par
ne plus être lue.

`panne` = un appelant qui téléphone maintenant n'est pas servi : la sonde répond **503**,
le job GitHub échoue, GitHub envoie un e-mail.

## Mise en place

1. Générer un jeton : `openssl rand -hex 32`.
2. Sur le VPS, l'ajouter au `.env` : `SUPERVISION_TOKEN=…`, puis
   `docker compose up -d api`. **Sans ce jeton la sonde répond 404** — délibérément :
   pas de jeton, pas de sonde, plutôt qu'une sonde ouverte à tous.
3. Sur GitHub : *Settings → Secrets and variables → Actions* → secret
   `SUPERVISION_TOKEN`, même valeur. (Facultatif : variable `SUPERVISION_URL` si le
   domaine change, secret `SUPERVISION_WEBHOOK` pour doubler l'e-mail d'un Slack/Discord.)
4. Éprouver l'alerte pour de vrai — *Actions → Supervision → Run workflow* — et vérifier
   que l'e-mail arrive **avant** d'en avoir besoin.

`scripts/deploy.sh` affiche le verdict après chaque déploiement, et signale l'absence de
jeton. C'est **informatif, jamais bloquant** : une sauvegarde en retard n'a pas à empêcher
de livrer un correctif, et une porte qui se ferme pour des motifs sans rapport finit par
être contournée.

## Métriques et Grafana (ASSISTANTE-156)

`/supervision` dit **si** ça va, à l'instant, toutes les quinze minutes. `/metrics` dit
**où** et **depuis quand** : des compteurs par établissement, relevés chaque minute par
Grafana Cloud, qui les garde et les trace. C'est là que se posent les alertes par client.

```
Grafana Cloud ──1 min──► GET /metrics  (Authorization: Bearer METRIQUES_TOKEN)
  relève, garde,           api/app/metriques.py
  trace, alerte            • compteurs en mémoire, ZÉRO appel réseau
                           • niveaux des 22 contrôles de supervision
```

Les mesures sont prises là où l'appel passe déjà — ouverture du flux (`main.py`), clôture
en base (`calls.finish_call`, commune à Pipecat et à GPT-Live), passage en secours
(`calls.ouvrir_secours`), vigie (un observateur). **Rien n'est ajouté sur le chemin de
l'audio**, et une mesure qui échoue ne fait jamais échouer un appel.

| Série | Ce qu'elle compte | Étiquettes |
|---|---|---|
| `helmane_appels_en_cours` | Appels dont le flux audio est ouvert | `etablissement` |
| `helmane_appels_termines_total` | Appels clos par issue : `servi`, `muet` (même règle que le contrôle « Appels muets »), `echec`, `secours` | `etablissement`, `moteur` (`pipecat`, `gpt-live`), `issue` |
| `helmane_secours_total` | Appels rendus au restaurant ou au répondeur, une fois par appel | `etablissement`, `motif` (`voix`, `modele`, `transcription`, `pipeline`…) |
| `helmane_erreurs_maillon_total` | Chaque erreur de la voix, du modèle ou de la transcription vue par la vigie — y compris celles qui ne renvoient pas l'appel | `etablissement`, `maillon` |
| `helmane_blanc_secondes` | Blancs ressentis, en histogramme ; 2,5 s et 4 s (les seuils de la supervision) sont des bornes de seau | `etablissement` |
| `helmane_appel_duree_secondes` | Durée des appels clos | `etablissement` |
| `helmane_supervision_niveau` | Niveau de chaque contrôle de `/supervision` (0 ok, 1 attention, 2 panne), et `controle="global"` | `controle` |
| `helmane_etablissement_info` | Le nom de chaque établissement, pour afficher des noms plutôt que des numéros | `etablissement`, `nom` |
| `process_*` | Mémoire et processeur du worker : ce qui dira qu'une machine ne suffit plus | — |

`etablissement` est l'identifiant de l'établissement ; l'établissement d'essai de la
recette porte `essai`, et le banc d'appels (`scripts/banc_appels.py`) `banc`, pour que
leurs appels automatiques ne passent pas pour un client. Chaque série d'un établissement
existe **à zéro** dès le démarrage : une série qui naîtrait à 1, au premier secours,
échapperait à `increase()`, donc à l'alerte.
**Aucune donnée personnelle** : ni numéro, ni propos, ni identifiant d'appel (un test
l'exige).

### Mise en place

Chaque étape écrit en production ou ouvre un service : **accord d'Helmi requis**.

1. Générer un jeton **distinct** de celui de la sonde : `openssl rand -hex 32`. Il sera
   confié à un tiers ; il doit pouvoir se révoquer seul.
2. Sur le VPS : `METRIQUES_TOKEN=…` dans le `.env`, puis `docker compose up -d api`.
   **Sans ce jeton, `/metrics` répond 404.**
3. Créer une pile Grafana Cloud, **en région européenne**, et y déclarer la relève de
   `https://<domaine>/metrics` toutes les minutes avec le jeton en *Bearer token*
   (intégration de relève d'un point de terminaison HTTP de Grafana Cloud — son nom et
   ses options sont à vérifier à la création du compte). À défaut, un agent Grafana
   Alloy ajouté à `docker-compose.yml` relèverait `api:8000/metrics` sur le réseau
   interne : un conteneur de plus sur la machine des appels, d'où ce second choix.
4. Importer le tableau de bord `docs/grafana/helmane.json` (*Dashboards → New →
   Import*), en choisissant la source Prometheus de la pile.
5. Poser les alertes ci-dessous, avec Telegram comme point de contact (ASSISTANTE-161),
   puis **éprouver** chacune : passer un appel muet sur l'établissement d'essai, vérifier
   que le message arrive.

### Alertes proposées

Les seuils sont des **propositions de départ**, pas des mesures : à régler sur le trafic
réel. Écarter l'établissement d'essai partout (`etablissement!="essai"`).

| Alerte | Requête | Pourquoi |
|---|---|---|
| L'application ne répond plus | `absent(helmane_supervision_niveau{controle="global"})` pendant 3 min | La relève échoue : machine, réseau ou Caddy. La panne qui compte le plus |
| Supervision en panne | `helmane_supervision_niveau{controle="global"} == 2` pendant 2 min | Le même verdict que `/supervision`, en 1 min au lieu de 15 |
| Appels muets | `sum by (etablissement) (increase(helmane_appels_termines_total{issue="muet"}[30m])) >= 2` | La panne du 30/07/2026, par client |
| Appels en secours | `sum by (etablissement) (increase(helmane_secours_total{motif!="vidange"}[10m])) > 0` | Un client n'a pas été servi par l'assistante. Les renvois d'une vidange de déploiement (ASSISTANTE-159) sont voulus : exclus. **Si la règle existe déjà chez Grafana Cloud, la mettre à jour** |
| Blanc qui dérive | `histogram_quantile(0.9, sum by (etablissement, le) (rate(helmane_blanc_secondes_bucket[30m]))) > 2.5` | Le seuil « attention » de la supervision, par client |
| Un maillon qui perd des phrases | `sum by (etablissement, maillon) (increase(helmane_erreurs_maillon_total[15m])) >= 5` | Une voix qui perd une phrase sur dix ne renvoie aucun appel : seule cette série la voit |

### Ce que les métriques ne voient pas

- **Les compteurs repartent de zéro à chaque redéploiement** : ils vivent dans le
  processus. Prometheus le sait (`rate`, `increase`) ; une valeur brute, elle, retombe.
- **Le blanc est compté à la clôture de l'appel**, pas tour par tour : un appel de dix
  minutes apparaît dix minutes plus tard. La jauge des appels en cours, elle, est
  immédiate.
- **Un appel renvoyé dès le décroché** (panne connue, `renvoi.py`) n'atteint jamais le
  pipeline : il est dans `helmane_secours_total`, pas dans `helmane_appels_termines_total`.
- **Les erreurs de GPT-Live** ne passent pas par la vigie : elles se lisent dans le
  contrôle « Crédit OpenAI » et dans `helmane_secours_total{motif="session"}`. Un appel
  GPT-Live interrompu en route est clos **avant** que le renvoi soit demandé : son issue
  dit `servi` ou `muet`, c'est le compteur de secours qui fait foi. Même chose pour un
  pipeline Pipecat qui lève une exception : issue `echec`, et un secours compté.

### Sonde externe chaque minute : étude (ASSISTANTE-162, relevé le 27/10/2026)

Le besoin : savoir en une minute, et non en quinze, que l'application ne répond plus ou
que `/supervision` dirait 503, par un surveillant qui ne tourne pas sur la machine.

**Ce que nous avons déjà, sans nouveau service.** La relève de Grafana Cloud ci-dessus
vient de l'extérieur chaque minute, et `/metrics` porte le verdict de `/supervision`
(`helmane_supervision_niveau{controle="global"}`, lu dans le même cache de 15 s). Les
deux premières alertes proposées font donc exactement la sonde demandée :
« L'application ne répond plus » (`absent(…)`, la machine, le réseau ou Caddy) et
« Supervision en panne » (`== 2`, l'équivalent du 503). Elles partent de chez Grafana :
une machine tombée ne les fait pas taire, contrairement à `app/telegram.py`.

**Recommandation : ne pas ouvrir de service de plus.** Finir la mise en place de Grafana
Cloud (étapes 1 à 5 ci-dessus), avec un point de contact Telegram configuré chez Grafana.
Pour une alerte par appel ou SMS, ajouter Grafana IRM, dans la même pile.
Le workflow `supervision.yml` reste le filet, toutes les 15 minutes.

| Service | Ce que la page de tarifs dit (relevé le 27/10/2026) | Pour nous |
|---|---|---|
| **Grafana Cloud** (recommandé) | Offre gratuite : 100 000 exécutions de tests d'API par mois en Synthetic Monitoring, IRM limité à 3 utilisateurs actifs par mois. Au-delà, 5 $ les 10 000 exécutions | Déjà prévu pour `/metrics`. Un test synthétique de `/health` chaque minute depuis un point de sonde : environ 44 000 exécutions par mois, sous le plafond gratuit. Il n'est utile que si la relève n'est pas posée. Le prix d'un SMS ou d'un appel par IRM n'est pas écrit sur la page de tarifs : à lire à l'ouverture |
| UptimeRobot | Gratuit : contrôle toutes les **5 min**, sans en-tête personnalisé ni Telegram. Solo : **60 s**, en-têtes personnalisés, Telegram ; 9 €/mois payé à l'année, 10 €/mois au mois. SMS et appels vocaux : crédits payants (10 crédits pour 3 $), 10 à 20 inclus en Solo | Le seul choix si Grafana Cloud n'est pas monté. L'offre gratuite ne peut pas porter le jeton de `/supervision` (`X-Supervision-Token`) |
| Better Stack | Gratuit : 10 moniteurs, alertes Slack et e-mail. La page ne donne ni la cadence de l'offre gratuite ni le prix de base de l'offre payante | Non retenu : rien à comparer sans ces chiffres |
| Healthchecks.io | Reçoit des signaux (surveillance de tâches planifiées), n'interroge pas une adresse | Ne répond pas au besoin |

Sources : `grafana.com/pricing`, `uptimerobot.com/pricing` (et sa version `pricing.md`),
`betterstack.com/pricing`, `healthchecks.io/pricing`, lues le 27/10/2026.

Ce qui reste à Helmi : ouvrir la pile Grafana Cloud et poser les alertes, ou à défaut
choisir UptimeRobot Solo. Puis l'essai du ticket : l'application arrêtée quelques
minutes hors des heures d'appels (une écriture en production, donc avec son accord) doit
déclencher l'alerte « ne répond plus ».

## Alertes Telegram

`api/app/telegram.py` pousse un message sur le téléphone de Helmi dès que l'application
constate une panne, sans attendre le cycle de 15 minutes du workflow GitHub (qui reste le
filet si l'application elle-même est tombée). Mise en place : créer un bot avec
@BotFather, écrire un message au bot, lire `result[0].message.chat.id` dans
`https://api.telegram.org/bot<jeton>/getUpdates`, puis poser `TELEGRAM_BOT_TOKEN` et
`TELEGRAM_CHAT_ID` dans le `.env` du serveur. Sans les deux, le module ne fait rien.

| Message | Quand | Épisode |
|---|---|---|
| Panne constatée | `renvoi.signaler_panne` | un par motif et par établissement, refermé avec la mémoire de la panne (3 min) |
| Appel passé en secours | `main._secours` | un par établissement et par motif, 30 min |
| Contrôle en panne / rétabli | `supervision.etat`, à chaque recalcul | jusqu'au retour au vert |
| Crédit OpenAI, solde Twilio | `notifications.py` | ceux de l'e-mail |

Une même alerte ne repart pas avant **15 minutes** (`REPRISE_SECONDES`), même si son épisode
s'est refermé : une panne qui dure, ou un contrôle qui oscille autour de son seuil, ne font
pas une rafale. « Rétabli » ne part qu'au vert, jamais sur une attention ni sur un contrôle
devenu impossible. Les essais de renvoi (bouton de l'admin, recette automatique) ne sonnent
pas. Un même fait peut donner deux ou trois messages (un compte Twilio suspendu : le solde,
puis les contrôles « Twilio » et « Solde Twilio ») : assumé, chacun dit sa source.

L'envoi ne bloque jamais l'appelant et ne lève jamais. Le jeton est dans l'URL de l'API
Telegram : un échec ne journalise que le nom de l'exception et le code HTTP. Les épisodes
sont en mémoire : un redémarrage de l'application peut répéter une alerte de panne qui dure.

## Erreurs dans Sentry

`api/app/erreurs.py` remonte à Sentry les erreurs de l'application, marquées de leur
établissement (`etablissement`) et de leur appel (`appel`) : la pile d'une erreur du flux
vocal et celle d'une tâche de fond qui finit en échec. Ce qui part et ce qui est retiré
(numéros, corps, cookies, variables locales, secrets) : `docs/RGPD.md` § 2. Mise en place :
ouvrir un compte (région UE), créer un projet Python, poser `SENTRY_DSN` (et, hors
production, `SENTRY_ENVIRONMENT`) dans le `.env` du serveur. Sans DSN, le module ne fait
rien. Ni traces de performance, ni journal de bord des requêtes : les erreurs seulement.
Un filtre qui échoue jette l'événement plutôt que de l'envoyer brut.

## Angles morts, assumés

- **Le TTS Modal n'est pas sondé activement.** Si le serveur de voix tombe et que
  personne n'appelle, rien ne le dit. Le prix de cette certitude serait un démarrage à
  froid du GPU par sonde. Au trafic actuel, c'est le bon arbitrage ; il cessera de
  l'être quand la voix sera vendue avec une garantie.
- **Le `cron` de GitHub est approximatif** (retards de plusieurs minutes aux heures
  chargées). C'est une supervision, pas une astreinte.
- **GitHub désactive les workflows planifiés après 60 jours sans activité** sur le
  dépôt — en silence. Un dépôt qui dort perd sa surveillance.
- **Le dépôt est public**, donc les journaux des runs le sont aussi : le résumé affiche
  des compteurs d'appels. Acceptable pour un prototype, à revoir avec de vrais clients.
- **L'e-mail part vers la personne qui a modifié le fichier de planification en dernier**
  (règle GitHub pour les workflows planifiés). À vérifier après chaque modification.

## Éprouver que ça mord

Trois mutations de `scripts/mutation_check.py` coupent le fil de l'alarme à trois
endroits — la sonde qui renvoie toujours 200, le verdict global forcé au vert, la
détection des appels muets désarmée. Chacune doit faire **rougir** les tests.
Une quatrième ouvre `/metrics` sans jeton : `tests/test_metriques.py` doit rougir.

### L'angle mort du 23/09/2026

Modal n'avait plus de GPU L4 en Europe. Cinq appels (145 à 149) ont entendu l'accueil, la
musique d'attente, puis plus rien — et **tous les contrôles sont restés verts**. « Appels
muets » comptait *n'importe quel* tour de l'assistante, or l'accueil est un WAV pré-rendu
joué **sans GPU** : il figurait en tête de chaque transcription. La même cécité masquait déjà
la panne du 30/07, que ce contrôle avait été écrit pour voir : la reprise et les relances, dites
sans le modèle, remplissaient la transcription.

Rejoué sur les 25 vrais appels 130 à 154 : l'ancien contrôle n'en signalait **aucun** ; le
nouveau signale les 5 appels de la panne **et l'appel 130** du 19/09, resté deux minutes sans
réponse pendant la rotation de la clé du serveur de voix — une panne passée inaperçue jusque-là.
Aucune fausse alerte.

On ne sonde **pas** le serveur de voix en direct : chaque sonde réveillerait le GPU et casserait
la mise en veille, qui fait l'essentiel des économies. La panne se lit dans les vrais appels.
Une supervision est le garde-fou le plus facile à rendre décoratif : elle a l'air de
marcher tant qu'on ne provoque pas la panne.
