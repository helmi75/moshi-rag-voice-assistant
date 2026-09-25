# resOS — le carnet de réservations du restaurant

> Epic **SCRUM-81**. Cette note est le livrable de **SCRUM-82** : ce que dit l'API, ce
> qu'on en utilise, ce qu'on a supposé faute de pouvoir l'essayer, et ce qu'il faudra
> vérifier au premier vrai appel.

## En une phrase

Un établissement réglé sur **resOS** voit les réservations prises au téléphone arriver
dans son carnet resOS, **comme des demandes à valider**. L'assistante ne dit jamais « c'est
confirmé », et **ne dit jamais « c'est enregistré »** si resOS ne l'a pas confirmé.

## D'où vient ce qu'on sait

| Source | État |
|---|---|
| Doc officielle : collection Postman « resOS API (v1.2) », mise à jour le 23/07/2025 — <https://documenter.getpostman.com/view/3308304/SzzehLGp> | ✅ lue en entier le 25/09/2026 : 19 routes, **chacune avec un exemple de réponse** |
| Bac à sable, compte de test, compte développeur | ❌ aucun, ni dans la doc ni sur le site |
| Accès à l'API | 💶 option à 11,99 €/mois, réservée aux formules payantes (Basic, 45 €/mois, et au-dessus). La formule gratuite (25 réservations/mois) ne l'inclut pas |
| Appel réel à l'API | ❌ **jamais fait** : pas de clé |

La doc complète se télécharge en JSON, sans compte (la page web, elle, est rendue en
JavaScript) :

```bash
curl -s "https://documenter.gw.postman.com/api/collections/3308304/SzzehLGp?segregateAuth=true&versionTag=latest" -o resos-api.json
```

Elle n'est pas copiée dans le dépôt : c'est la documentation de resOS, pas la nôtre, et
elle évolue (« We are extending the API as needed »).

## Les 5 questions de SCRUM-82

| # | Question | Réponse | État |
|---|---|---|---|
| 1 | Peut-on connaître les disponibilités **avant** de réserver ? | **Oui.** `GET /bookingFlow/times?date=AAAA-MM-JJ&people=N` rend un objet par service, avec `availableTimes` et `unavailableTimes`. `GET /bookingFlow/dates` ne tient pas compte du complet (« not if the day is fully booked ») | ✅ documenté |
| 2 | Comment retrouver les réservations d'un numéro ? | `GET /bookings?customQuery=guest.phone:"33612345678"`, avec l'indicatif en tête et sans le + (« the country code must be included as the first 2 digits »), plus `fromDateTime` | ✅ documenté |
| 3 | Webhook, ou interrogation régulière ? | **Pas de webhook** (« We are currently working on webhooks »). On **lit resOS au moment de l'appel**, sans copie locale : resOS reste la seule source de vérité | ✅ documenté |
| 4 | Limites de débit, taille de page ? | « 100 requests / sec, with a fixed-time reset (i.e. every minute) » : formulation ambiguë, par seconde ou par minute ? 100 résultats par page au maximum, en-tête `Retry-After` sur un 429 | ⚠️ question posée à resOS |
| 5 | Que rend l'API quand le créneau est complet ? | **Inconnu.** `POST /bookings` ne rend que l'identifiant. Une erreur n'est documentée que si l'on impose une zone (`areaId`) sans table libre | ❌ à tester — on s'en protège (voir plus bas) |

## Qui fait foi : la décision de synchronisation

**resOS fait foi, et on ne copie rien.** Sans webhook, une copie locale serait fausse dès
que le restaurant touche à une réservation dans resOS. Chaque outil lit ou écrit resOS
au moment où l'appelant le demande. Ce qui reste **chez nous**, quel que soit le carnet :
le journal de l'appel (transcription, latences, coût, enregistrement) et les messages
pris pour l'équipe.

## Ce que l'assistante fait dans resOS

| Outil | Appel resOS | Ce que lit le modèle |
|---|---|---|
| `check_availability` | `GET /bookingFlow/times` | `available`, et sinon les 3 horaires libres les plus proches |
| `create_reservation` | `GET /bookingFlow/times`, **puis** `POST /bookings` | `pending_restaurant_approval` + consigne « ne dis pas confirmé » |
| `find_reservation` | `GET /bookings?customQuery=guest.phone:…` | les réservations à venir de **ce** numéro |
| `modify_reservation` | `GET /bookings/{id}`, créneau si l'heure change, `PUT /bookings/{id}`, note interne si besoin | la réservation relue après modification |
| `cancel_reservation` | `GET /bookings/{id}`, `PUT {"status": "canceled"}` | `cancelled` |

Une réservation créée par l'assistante porte :

- `status: "request"`, **à valider par le restaurant** (décision de Helmi, 25/09/2026) ;
- `source: "phone"`, une valeur prévue par resOS ;
- `guest.phone` : le numéro de l'appelant, fourni par le réseau téléphonique, jamais
  par le modèle ;
- une note interne « Prise au téléphone par l'assistante. », visible du seul restaurant.
  Les demandes du client (« table au calme ») vont dans `comment` ;
- `notificationSms` et `notificationEmail` à `false` : resOS n'envoie ses messages
  qu'en anglais, espagnol, allemand, danois ou suédois. Un client français recevrait
  un SMS en anglais.

## Les trois règles du connecteur (`api/app/connecteurs/resos.py`)

1. **Ne jamais annoncer ce qui n'est pas sûr.** Plus de 4 s de bout en bout, erreur
   réseau, 5xx, 429, clé refusée ou absente : le modèle reçoit « N'annonce RIEN comme
   enregistré », et prend un message pour l'équipe. Une écriture coupée a peut-être
   abouti chez resOS : on préfère une demande en double, que le restaurant voit, à une
   confirmation inventée.
2. **Vérifier le créneau juste avant d'écrire.** C'est la parade à la question 5 : si
   resOS accepte en silence une réservation sur un créneau complet, le restaurant
   recevrait une demande qu'il ne peut pas honorer.
3. **Ne rendre à un appelant que SES réservations.** Le filtre `customQuery` n'a jamais
   été éprouvé : le numéro est revérifié chez nous sur chaque réservation rendue.
   L'identifiant proposé par le modèle est échappé avant d'entrer dans l'URL.

## Mettre un restaurant sur resOS

1. Le restaurant active l'app **API** dans resOS (menu → Apps), puis génère une clé
   (Réglages → API credentials).
2. Sur le serveur, dans le `.env` : `RESOS_API_KEYS=<id de l'établissement>=<clé>`.
   Plusieurs établissements sont séparés par des virgules. Puis on redéploie.
3. Dans l'admin (super-admin), fiche de l'établissement → **Carnet de réservations :
   resOS**. La fiche indique si la clé est posée, sans jamais l'afficher.

## Le faux resOS

`api/tests/faux_resos.py` reproduit la doc, avec des réponses à la forme exacte des
exemples. Les tests (`test_connecteurs.py`) passent par lui. En tête de fichier, deux
listes séparent ce qui est **documenté** de ce qui est **supposé**. Pour un banc local :

```bash
cd api && python -m uvicorn tests.faux_resos:app --port 8765
```

puis `RESOS_API_URL=http://localhost:8765/v1` et `RESOS_API_KEYS="<id>=cle-de-test-resos"`.

## À vérifier au premier vrai appel (clé d'un restaurant pilote)

- [ ] la réponse de `POST /bookings` sur un créneau complet (question 5) ;
- [ ] la limite de débit : 100 par seconde ou 100 par minute (question 4) ;
- [ ] `customQuery` passé en paramètre d'URL, comme dans les exemples. La doc dit aussi
      « passed as a header » ;
- [ ] `fromDateTime` au format date seule (`AAAA-MM-JJ`), comme dans l'exemple ;
- [ ] la `duration` (120 min imposées) : resOS applique-t-il celle du service ?
- [ ] la latence réelle d'un `bookingFlow/times` suivi d'un `POST`, mesurée au journal
      de bord : c'est du silence au téléphone ;
- [ ] une réservation créée apparaît bien en « demande » dans l'interface de resOS.

## Ce qui ne marche pas encore

| Manque | Ticket |
|---|---|
| Les horaires et fermetures viennent de resOS pour les disponibilités, mais le prompt ne les cite pas encore | SCRUM-86 |
| resOS injoignable : le modèle prend un message, mais ni la supervision ni le restaurateur n'en sont prévenus | SCRUM-87 |
| La page Réservations de l'admin et le compteur « réservations » du tableau de bord ne lisent que notre carnet. Les appels, eux, comptent bien ceux qui ont réservé dans resOS | à planifier |
| Le nom du dernier passage n'est pas proposé aux clients resOS : ce serait une requête réseau avant le décroché | à mesurer avec une vraie clé |
