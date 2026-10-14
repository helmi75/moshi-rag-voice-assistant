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

## Les dix-huit contrôles

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
| Alertes Twilio | Webhook injoignable, TwiML invalide — **invisible de l'intérieur**. Relevées dans chaque région dont on a le jeton ; une région qui le refuse est nommée (« relève impossible en ie1 (HTTP 401) ») sans faire taire l'autre | non |
| Carnet resOS | Établissements dont les réservations vont dans resOS : clé API absente, ou resOS en échec (état écrit sur l'ardoise à chaque changement, il survit aux redémarrages). Pendant la panne, l'assistante prend des messages au lieu de réserver. Le bac à sable plafonne à « attention » : une panne simulée ne réveille pas l'alerte | oui si dernier échec < 1 h |
| Voix Mistral | La voix de tous les établissements (Voxtral) : clé `MISTRAL_API_KEY` absente (voix de secours Moshi, réveil de GPU), ou synthèses échouées dans l'heure — chacune est une phrase que l'appelant n'a pas entendue. On ne sonde pas Mistral : chaque sonde serait facturée | oui si la clé manque |
| Appels passés en secours | Des appels que l'assistante n'a pas pu servir (ASSISTANTE-118) : renvoyés au restaurant, passés au répondeur, ou repartis sans personne — une panne de fournisseur de dix minutes ne laisse rien d'autre dans la base. Attention aussi si un établissement n'a pas de numéro de secours. Les essais lancés depuis l'admin ne comptent pas | oui si un appel est passé en secours dans les 20 dernières minutes |
| Crédit OpenAI | Un refus de session GPT-Live pour crédit épuisé (ASSISTANTE-151), vu depuis le démarrage : l'heure du premier refus, le nombre de refus, et la marche à suivre (recharger le crédit dans la console OpenAI ; la chaîne classique sert les appels en attendant). **Ce n'est pas une mesure du solde** — OpenAI ne le donne pas par son API : « OK » veut dire « aucun refus vu ». Revient au vert dès qu'une session s'ouvre. Un e-mail part aux super-admins au premier refus de l'épisode. Sans clé GPT-Live : OK, rien à surveiller. Mémoire du processus : un redéploiement l'efface | oui dès le premier refus Deux heures sans nouveau refus : l'alerte descend à « attention » (rien ne dit si le crédit est revenu), pour ne pas masquer les autres pannes ; elle ne s'efface qu'à la première session ouverte. |
| Voix d'accueil | WAV non pré-rendu : décroché non instantané (démarrage à froid du GPU) ; message « rappelez dans quelques minutes » absent — si le GPU ne vient pas, l'appelant n'entendra que le silence | non |
| Sauvegarde | Fraîcheur du jeton écrit par `backup-db.sh` **après** le contrôle d'intégrité ; copie hors du serveur absente ou arrêtée (attention) | oui à 72 h |
| Purge des données personnelles | Deux cycles manqués = la durée annoncée au registre n'est plus tenue | oui |
| Espace disque | Un disque plein laisse passer les `SELECT` et fait échouer la seule chose qui compte : enregistrer une réservation | oui sous 2 Go |
| Enregistrement des appels | Activé mais n'aboutit pas — « 0 sur 12 » est le cas qu'on veut voir | non |

Un contrôle qui échoue lui-même est signalé « état INCONNU », jamais vert : une panne de
supervision ne doit pas se déguiser en bonne nouvelle.

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
                           • niveaux des 18 contrôles de supervision
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
recette porte `essai`, pour que ses appels automatiques ne passent pas pour un client.
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
| Appels en secours | `sum by (etablissement) (increase(helmane_secours_total[10m])) > 0` | Un client n'a pas été servi par l'assistante |
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
- **Les erreurs de GPT-Live** ne passent pas par la vigie : elles se lisent dans l'issue
  des appels (`moteur="gpt-live"`) et dans le contrôle « Crédit OpenAI ».

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
