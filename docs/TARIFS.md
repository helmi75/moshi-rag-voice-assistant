# Grille tarifaire (#29, ASSISTANTE-120)

Arrêtée le **02/10/2026** (validée par Helmi), en remplacement de la grille en appels du
30/08/2026. Source de vérité pour le code : `api/app/plans.py`.
Ce document explique **pourquoi** ces chiffres, et ce qui les invaliderait.

## La grille

Prix **hors taxes**. Les minutes sont **décomptées à la seconde**.

| Formule | Abonnement | Minutes incluses | Minute en plus | Établissements | Pour qui |
|---|---|---|---|---|---|
| **Liberté** | 0 € (10 € de mise en service par numéro, une fois) | aucune | 0,45 € | 1 | Celui qui veut essayer sans s'abonner, ou qui reçoit peu d'appels |
| **Essentiel** | 89 €/mois | 250 | 0,35 € | 1 | Un restaurant qui rate ses appels au coup de feu |
| **Service** | 149 €/mois | 600 | 0,25 € | 1 | Un restaurant qui vit du téléphone, midi et soir |
| **Maison** | 349 €/mois | 1 500 | 0,20 € | 5 | Un groupe ou une enseigne à plusieurs adresses |

Alerte au restaurateur à **80 %** du forfait. Le forfait compte, prévient et facture : il
**ne coupe jamais la ligne** (`api/app/quotas.py`).

Pas d'« illimité ». Jamais. Le coût d'une minute est linéaire, pas le prix.

### Ce qui est décompté

- Le mois **calendaire**, à l'heure du restaurant.
- La durée de chaque appel clos, à la seconde : trois appels de quarante secondes font
  deux minutes, pas trois minutes entamées.
- Un appel compte même sans réservation : il a consommé la même voix et la même
  transcription.
- Un appel que l'assistante **a rendu parce qu'elle était en panne** (renvoi vers le
  restaurant, ASSISTANTE-118) n'est **pas** décompté : sa durée est celle que le
  restaurant a passée à son propre téléphone.

### Quand changer de formule

| Passage | Devient moins cher à partir de |
|---|---|
| Liberté → Essentiel | 198 minutes par mois (89 € ÷ 0,45 €) |
| Essentiel → Service | 421 minutes par mois (89 € + 171 min à 0,35 € ≈ 149 €) |
| Service → Maison | 1 400 minutes par mois (149 € + 800 min à 0,25 € = 349 €) |

La minute en plus baisse à chaque palier, et monter de formule redevient toujours
avantageux avant d'avoir doublé sa facture. `test_plans.py` tient ces propriétés.

## Pourquoi des minutes, et plus des appels

Jusqu'au 02/10/2026 la grille vendait des appels (150, 400, 750). Deux raisons de changer :

1. **Les concurrents comptent tous en minutes.** Un restaurateur ne pouvait pas comparer
   « 150 appels » à « 150 minutes ». Et à 3,5 minutes par appel, 150 appels faisaient
   525 minutes pour 89 € : deux à trois fois ce que donne le marché au même prix.
2. **Un forfait en appels nous faisait porter la durée.** À 8 minutes de moyenne, la
   formule Service tombait à 26 % de marge. À la minute, c'est la consommation réelle
   qui est facturée, quelle que soit la durée d'un appel.

## Le coût, mesuré et non supposé

| Poste | Tarif | Source |
|---|---|---|
| Twilio, numéro français, appel reçu | 0,01 $ par minute **entamée** | factures des appels 183 à 204 |
| Deepgram nova-3 en flux, `multi` | 0,0092 $/min | [deepgram.com/pricing](https://deepgram.com/pricing), vérifié le 30/08/2026 |
| Voix Mistral (Voxtral) | 0,016 $ pour 1 000 caractères, ~320 caractères par minute d'appel | mistral.ai ; mesuré sur les appels 202 à 204 |
| Modèle de langage (via OpenRouter) | compté par appel, en jetons | **à mesurer** sur un mois d'appels réels ; 0,35 c$ par appel retenu faute de mieux |
| Numéro français | 1,35 $/mois | console Twilio |

Relevé du **28/09/2026**, pour un appel de 3,5 minutes : **≈ 9,4 c$, soit ≈ 8,7 c€**
(1 € = 1,08 $ — le taux dérive, le recalculer avant toute décision qui en dépend).
Rapporté à la minute : **≈ 2,5 c€**.

Un appel court coûte un peu plus cher à la minute que cette moyenne, parce que Twilio
facture la minute entamée et que nous décomptons à la seconde : environ 3 c€ la minute
pour un appel de 45 secondes. `test_plans.py` retient une borne haute de **5 c€** et
exige que chaque minute soit vendue plus du double.

## Les marges

Formule utilisée à plein, à 2,5 c€ la minute, numéros compris (1,25 € par numéro) :

| Formule | Prix | Coût mensuel | Marge | Prix de la minute incluse |
|---|---|---|---|---|
| Liberté | 0,45 € la minute | 2,5 c€ la minute | **94 %** | — |
| Essentiel | 89 € | 7,50 € | **81,50 € · 92 %** | 0,36 € |
| Service | 149 € | 16,25 € | **132,75 € · 89 %** | 0,25 € |
| Maison | 349 € | 43,75 € | **305,25 € · 87 %** | 0,23 € |

Minute en plus : 93 % de marge à 0,35 €, 90 % à 0,25 €, 87,5 % à 0,20 €.

C'est une marge sur les **coûts variables** : ni le serveur, ni le temps de mise en
service et de support, ni les frais de paiement, ni le coût d'acquisition d'un client.

Si la minute coûtait le double (5 c€) : 85 %, 79 % et 77 %.

## Le marché

Relevé sur les sites des concurrents le **02/10/2026**, prix hors taxes sauf mention.

| Concurrent | Abonnement | Minutes incluses | Minute en plus |
|---|---|---|---|
| Sylen Start | 49 € | 100 | 0,44 € |
| Sylen Pro | 129 € | 350 | 0,39 € |
| Sylen Scale | 329 € | 1 000 | 0,35 € |
| Yumcall | 99 € (carnet de réservations et site inclus ; HT ou TTC non précisé) | 150 | packs : +200 min à 39 €, +500 à 89 €, +1 000 à 159 € |
| QTable (agent vocal) | 149 €, engagement 3 mois | 200 | non affiché |

Aucun des trois n'affiche de frais de mise en service.

Relevé du **30/09/2026**, non relu le 02/10 : Kouver 89 € + 5 €/mois + 0,30 €/min et des
frais de mise en place ; Limova 140 €/mois + 0,20 €/min ; Zenchef AI Concierge 99 €/mois
en plus d'un abonnement Zenchef (129 à 249 €), minutes non précisées.

### La facture du restaurateur, selon ses minutes

| Minutes par mois | Helmane | Sylen | Yumcall |
|---|---|---|---|
| 100 | **45 €** (Liberté) | 49 € | 99 € |
| 200 | **89 €** (Essentiel) | 93 € | 138 € |
| 350 | **124 €** (Essentiel) | 129 € | 138 € |
| 500 | **149 €** (Service) | 187,50 € | 188 € |
| 1 000 | **249 €** (Service) | 329 € | 258 € |

**La seule zone où Helmane n'est pas le moins cher** : entre 1 040 et 1 150 minutes pour
un seul restaurant, Yumcall coûte jusqu'à 28 € de moins (258 € avec son pack de
1 000 minutes).

## 🔴 Ce qui n'est pas su

- **Combien de minutes consomme un vrai restaurant.** Aucune mesure : la seule durée
  connue, 3,5 minutes par appel, vient de 24 appels de test, les nôtres. Le forfait
  Essentiel (250 minutes) fait environ 70 appels de cette durée ; on ne sait pas si
  c'est peu ou beaucoup pour un restaurant parisien. La page d'accueil affiche cet
  équivalent (70, 170, 430 appels) en disant d'où il vient ; il se règle par
  `plans.DUREE_APPEL_MIN`.
- **Le coût du modèle de langage par appel**, encore au forfait dans ce calcul.
- **Si les packs de Yumcall se cumulent**, et comment Sylen décompte ses minutes.
- **La formule Maison n'est pas applicable** tant que rien ne regroupe plusieurs
  établissements sous un même client (voir la note en bas de `api/app/quotas.py`) : le
  compteur est par établissement.
- **Un client Liberté qui n'appelle plus** coûte le numéro (1,25 € par mois) sans rien
  rapporter. Règle envisagée, non codée : suspendre la ligne après 60 jours sans appel.

➡️ **À faire dès le premier restaurant pilote (#32)** : relever sur un mois les minutes
consommées et le coût moyen de la minute (« Santé & coûts »), et revenir ici.

## La grille précédente (30/08 au 02/10/2026)

Essentiel 89 € pour 150 appels, Service 149 € pour 400, Maison 349 € pour 750, et 0,30 €
par appel au-delà. Marges à 3,5 minutes par appel, au coût du 28/09 : 84 %, 76 % et 80 %.
Aucun client n'a été facturé sur cette grille.
