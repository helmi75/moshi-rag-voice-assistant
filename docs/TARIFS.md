# Grille tarifaire (#29)

Arrêtée le **30/08/2026**. Source de vérité pour le code : `api/app/plans.py`.
Ce document explique **pourquoi** ces chiffres, et ce qui les invaliderait.

## La grille

| Formule | Prix | Appels inclus | Établissements | Pour qui |
|---|---|---|---|---|
| **Essentiel** | 89 €/mois | 150 | 1 | Un restaurant qui rate ses appels au coup de feu |
| **Service** | 149 €/mois | 400 | 1 | Un restaurant qui vit du téléphone, midi et soir |
| **Maison** | 349 €/mois | 750 | 5 | Un groupe ou une enseigne à plusieurs adresses |

**Dépassement : 0,30 € par appel.** Alerte au restaurateur à **80 %** du plafond.

Pas d'« illimité ». Jamais. Le plafond est ce qui empêche un établissement à 800 appels
par mois de manger la marge GPU de tous les autres.

## Le coût, mesuré et non supposé

| Poste | Tarif | Source |
|---|---|---|
| Twilio (entrant) | 0,0085 $/min | mesuré, `scripts/cost_report.py` |
| Deepgram nova-3 streaming, monolingue | 0,0077 $/min | [deepgram.com/pricing](https://deepgram.com/pricing), vérifié le 30/08/2026 |
| Modal GPU L4 (voix Moshi) | 0,020 $/min | mesuré |
| LLM (gemini-flash via OpenRouter) | 0,0035 $/appel | mesuré |
| Numéro FR | 1,35 $/mois | console Twilio |

⚠️ **Le tarif Deepgram a été corrigé ce jour-là** : le code portait 0,0058 $, qui est le
tarif **nova-2**, alors que la production tourne en **nova-3**. Écart de +33 % sur la
ligne transcription. Repasser `DEEPGRAM_LANGUAGE=multi` ferait remonter le tarif à
0,0092 $/min — il faudrait alors ajuster `COST_DEEPGRAM_PER_MIN`, sinon l'admin
sous-estime en silence.

### Coût d'un appel selon sa durée

| Durée | Coût |
|---|---|
| 1,5 min | 5,4 c€ |
| 2 min | 7,0 c€ |
| **3,5 min** (moyenne mesurée) | **12,1 c€** |
| 5 min | 17,1 c€ |

*Taux retenu pour la conversion : 1 € = 1,08 $. Il dérive — le recalculer avant toute
décision qui en dépend, plutôt que de le figer dans le code.*

## Les marges

À 3,5 min de moyenne, numéros compris :

| Formule | Prix | Coût mensuel | Marge | Tarif implicite |
|---|---|---|---|---|
| Essentiel | 89 € | 19 € | **70 € · 78 %** | 0,59 €/appel |
| Service | 149 € | 49 € | **100 € · 67 %** | 0,37 €/appel |
| Maison | 349 € | 97 € | **252 € · 72 %** | 0,47 €/appel |

### Ce qui a été corrigé par rapport à la proposition initiale

La formule Maison était proposée à **249 € pour 1 200 appels** : 151 € de coût, soit
**39 % de marge** — la formule la plus chère à servir était la moins rentable. Ramenée à
750 appels et 349 €, elle revient dans la norme des deux autres.

## Le dépassement, et son incitation à l'envers

**0,30 €/appel = 60 % de marge** au coût mesuré. Mais c'est **moins** que le tarif
implicite d'Essentiel (0,59 €). Conséquence assumée : un client Essentiel a intérêt à
déborder plutôt qu'à passer en Service.

- à 300 appels sur Essentiel : 89 € + 45 € = **134 €**, contre 149 € en Service ;
- la marge tient quand même — **72 %** dans ce cas.

**On perd de l'upsell, pas de la marge.** Passer le dépassement à 0,50 € inverserait
l'incitation (monter de formule redeviendrait toujours moins cher) au prix d'une facture
de dépassement plus dure à faire accepter. Décision commerciale, révisable à tout moment
via `plans.DEPASSEMENT_EUR`.

## Le marché

| Concurrent | Prix |
|---|---|
| Accueil IA | 39 à 149 € |
| Yumcall | 99 € |
| Nerolia | à partir de 149 € |
| Loman (US) | ≈ 299 $ + 149 $ d'installation |
| Slang.ai (US) | à partir de 399 $ |

Essentiel à 89 € se place juste sous Yumcall ; Service à 149 € est au niveau de Nerolia.
La grille est dans le marché, sans casser les prix.

## 🔴 Le risque, assumé et à lever

**La durée moyenne de 3,5 minutes vient de 24 appels de test**, les nôtres,
exploratoires. Un vrai « vous êtes ouverts ce soir ? » dure 45 secondes ; à l'inverse une
réservation à négocier peut durer 6 minutes.

C'est le seul chiffre de ce document qui n'est pas solide, et **c'est celui dont tout
dépend** :

| Durée moyenne réelle | Essentiel | Service | Maison |
|---|---|---|---|
| 2 min | 87 % | 80 % | 83 % |
| **3,5 min** (retenu) | **78 %** | **67 %** | **72 %** |
| 5 min | 70 % | 53 % | 61 % |
| 8 min | 53 % | 26 % | 40 % |

**La formule la plus exposée est Service, pas Maison.** C'est contre-intuitif — on
surveille spontanément la formule la plus chère — mais Service inclut 400 appels pour un
seul établissement, soit la plus forte densité d'appels par euro facturé. Elle plonge à
26 % de marge si les appels durent 8 minutes.

La grille tient jusqu'à **5 minutes** de moyenne. Au-delà, deux leviers : relever les
prix, ou raccourcir les appels — le prompt système y joue directement, et c'est le levier
gratuit.

➡️ **À faire dès le premier restaurant pilote (#32)** : mesurer la durée moyenne réelle
sur 100 appels et revenir ici. `/admin/health` affiche déjà la donnée.
