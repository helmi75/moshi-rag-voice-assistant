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

## Les neuf contrôles

| Contrôle | Ce qu'il attrape | Panne |
|---|---|---|
| Base de données | Volume plein ou en lecture seule. Il **écrit** vraiment : lire prouve que la base répond, pas qu'elle accepte encore une réservation | oui |
| Configuration du chemin d'appel | Une variable manquante selon le mode en vigueur, ou une `PUBLIC_WS_URL` contenant une espace — l'appel raccroche alors sans un mot | oui |
| Appels muets | Appel `completed`, plus de 15 s, **pas un tour** de l'assistante. La signature exacte du 30/07 | si majoritaire |
| Appels en échec | Le pipeline a levé une erreur pendant l'appel | si majoritaire |
| Appels jamais clôturés | `finish_call` n'a pas tourné : le worker est mort avec l'appel | si majoritaire |
| Blanc ressenti | Dérive de la latence (mesuré en prod : 1,16 s ; attention à 2,5 s, panne à 4 s) | oui |
| Alertes Twilio | Webhook injoignable, TwiML invalide — **invisible de l'intérieur** | non |
| Voix d'accueil | WAV non pré-rendu : décroché non instantané (démarrage à froid du GPU) | non |
| Sauvegarde | Fraîcheur du jeton écrit par `backup-db.sh` **après** le contrôle d'intégrité | oui à 72 h |

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
Une supervision est le garde-fou le plus facile à rendre décoratif : elle a l'air de
marcher tant qu'on ne provoque pas la panne.
