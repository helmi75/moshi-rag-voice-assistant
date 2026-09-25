# Recette — ce qu'il faut vérifier le jour où la ligne sonne

> **Pourquoi ce document existe.** Six chantiers ont été livrés entre le 23 et le
> 31/08/2026 — supervision, grille tarifaire, plafond, RGPD, modification par téléphone —
> et **aucun n'a été éprouvé sur un appel réel**. Le dernier vrai appel date du
> **09/08/2026**. Les tests couvrent la logique ; ils ne disent rien de ce qu'un client
> entend.
>
> Écrire la liste maintenant, à froid, vaut mieux que l'improviser le jour où la ligne
> sonne — et que découvrir trois semaines plus tard qu'on a oublié de regarder.
>
> Préalable unique : **compte Twilio actif** (suspendu au 31/08, solde d'essai négatif).

## Comment s'en servir

Chaque test tient en un appel. Note le résultat dans la colonne, et **ce que tu as
réellement entendu** — pas ce que tu attendais. Un « ça marche » sans citation n'apprend
rien à la session suivante.

Pendant les appels, garde ceci ouvert dans un terminal :

```bash
ssh root@187.77.172.87 "docker compose -f /opt/moshi-rag-voice-assistant/docker-compose.yml logs -f --tail=20 api"
```

---

## A. Le socle — sans ça, rien d'autre n'a de sens

| # | Test | Attendu | ✅/❌ |
|---|---|---|---|
| A1 | Appeler le numéro | Ça décroche | |
| A2 | Écouter le tout début | L'accueil du restaurant, **puis** « Cet accueil est assuré par un assistant vocal ; votre appel est traité pour votre réservation » | |
| A3 | Chronométrer le blanc avant la 1ʳᵉ réponse | < 2 s si le GPU est chaud ; ≈ 46 s au tout premier appel de la journée, couvert par la musique d'attente | |
| A4 | Vérifier le journal | `POST /twilio/webhook` puis `WebSocket /ws/voice [accepted]` | |
| A5 | **Commencer en français**, échanger deux phrases, **puis passer à l'anglais** | Elle répond en anglais. Elle peut manquer UN tour le temps de rouvrir l'oreille — pas plus, et surtout pas jusqu'à la fin de l'appel | |
| A6 | Dans le même appel, revenir au français | Elle suit, sans qu'on ait à répéter trois fois | |
| A7 | Relire la transcription dans l'admin | L'accueil apparaît **une seule fois** ; un nom déjà connu est écrit « Helmi », pas « HELMI » | |
| A8 | Liste des appels, quelques minutes après | Une phrase de résumé (« Réservation pour deux… »), pas « Bonjour, restaurant… » | |
| A9 | GPU introuvable : `MOSHI_HOLD_MAX_SECONDS=5` dans le `.env`, redémarrer, attendre 3 min sans appel (GPU éteint), appeler — **remettre la variable ensuite** | Accueil, 5 s de musique, puis « Toutes nos excuses… merci de nous rappeler dans quelques minutes. Au revoir. » et l'appel raccroche — jamais de silence | |

**A2 est le test de #22.** Si la mention manque, la migration n'est pas passée ou
`RGPD_MENTION` est à 0.

**A5 est le test du 21/09.** C'est le défaut le plus coûteux qu'on ait entendu :
l'assistante devenait sourde pour tout le reste de l'appel. Le journal le dit en toutes
lettres — `langue de l'appel : 2 tours de parole sans aucune transcription … retour au
bilingue`.

## B. Prendre une réservation — la fonction historique

| # | Test | Attendu | ✅/❌ |
|---|---|---|---|
| B1 | « Une table pour 2 demain à 20h, au nom de Martin » | Récapitulatif, puis confirmation **après** l'outil | |
| B2 | Regarder l'admin | La réservation, avec **ton numéro** en téléphone | |
| B3 | Appeler en numéro masqué (`#31#` devant) et réserver | La réservation existe, **téléphone vide** | |

**B2 est le test de la correction du 31/08** : le téléphone doit venir du réseau, pas du
modèle. S'il est vide alors que tu appelais en clair, `caller_number` ne parvient pas
jusqu'à `run_tool`.

## C. Modifier et annuler — #33, jamais éprouvé

| # | Test | Attendu | ✅/❌ |
|---|---|---|---|
| C1 | « Je voudrais décaler ma table de demain à 21h30 » | Elle retrouve la réservation **sans te demander de numéro de dossier** | |
| C2 | Vérifier l'admin | L'heure a changé, la date n'a pas bougé | |
| C3 | « Finalement je préfère annuler » | Elle fait confirmer, puis annule | |
| C4 | Admin → Réservations | Ligne **barrée**, chip « Annulée », date d'annulation | |
| C5 | Rappeler et redemander le même créneau | Il est **de nouveau libre** | |
| C6 | Appeler en masqué et demander une annulation | Elle prend le message, **n'annule rien**, n'invente pas | |

**C5 est le piège silencieux** : si le créneau est refusé, `ACTIVES` ne s'applique pas
partout et une table annulée compte encore.

**C1 est le vrai test de langage.** Écoute comment elle formule quand il y a plusieurs
réservations, et **note ses mots** : c'est ce qui permettra d'affiner le prompt.

## D. Ce que ça coûte vraiment — la question qui décide de la grille

| # | Test | Attendu | ✅/❌ |
|---|---|---|---|
| D1 | Faire **10 appels réalistes** (durée normale, pas d'exploration) | | |
| D2 | Admin → Santé & coûts → durée moyenne | **C'est LE chiffre.** Toute `docs/TARIFS.md` repose sur 3,5 min mesurées sur 24 appels de test exploratoires | |
| D3 | Comparer au tableau de sensibilité de `docs/TARIFS.md` | Si > 5 min : la formule **Service** tombe sous 53 % de marge, à revoir | |
| D4 | Blanc médian | Comparer à 1,16 s (mesuré le 30/07) | |

## E. Plafond et facturation — #31

| # | Test | Attendu | ✅/❌ |
|---|---|---|---|
| E1 | Admin → fiche établissement | Le sélecteur de **Formule** apparaît (super-admin seulement) | |
| E2 | Se connecter en restaurateur | Le sélecteur **n'apparaît pas** | |
| E3 | Salle de contrôle | Carte « Forfait », compteur du **mois calendaire** | |
| E4 | Éprouver le plafond sans passer 150 appels : sur le VPS, `SUPERVISION_*` n'y peut rien — il faut une formule de test à faible plafond, ou insérer des lignes `calls` du mois en cours directement en base | Alerte à 80 %, puis « Plafond dépassé » avec le montant — **et la ligne continue de répondre** | |

**E4 est la décision produit du 30/08** : atteindre le plafond ne coupe jamais la ligne.

## F. Supervision — #24, l'alerte a été éprouvée, pas le contenu

| # | Test | Attendu | ✅/❌ |
|---|---|---|---|
| F1 | Après les appels, interroger la sonde | `niveau: ok` | |
| F2 | Contrôle « Appels muets » | Passe de « pas de mesure » à un vrai comptage | |
| F3 | Contrôle « Alertes Twilio » | Passe au vert une fois le jeton renouvelé | |
| F4 | Contrôle « Purge » | « Passée il y a N h » | |
| F5 | Contrôle « Espace disque » | Vert, avec le volume des enregistrements | |
| F6 | Contrôle « Enregistrement des appels » | `N / N` — « 0 sur 12 » signifie cassé en silence | |

```bash
curl -s -H "X-Supervision-Token: $SUPERVISION_TOKEN" https://app.helmane.fr/supervision | jq
```

## F bis. Diagnostiquer un appel raté (#88) — le cœur du sujet

Après les appels du bloc D, ouvrir **Journal des appels → un appel → Diagnostic**.

| # | Test | Attendu | ✅/❌ |
|---|---|---|---|
| H1 | Le lecteur audio joue | On entend l'appel. **En stéréo** : soi-même à gauche, l'assistante à droite | |
| H2 | Provoquer une coupure : parler pendant qu'elle parle | La chronologie affiche « Le client a coupé » sur le bon tour, et on l'ENTEND sur les deux canaux | |
| H3 | Sur le tour le plus lent, lire la décomposition | Un seul étage domine. **C'est la réponse** : STT, compréhension, outil ou voix | |
| H4 | Prononcer un nom difficile (« Nguyen », « Kowalski ») | La chronologie montre le texte entendu et, s'il est douteux, « Transcription peu sûre » | |
| H5 | Comparer « Entendu » et ce que tu as réellement dit | S'ils diffèrent : problème de STT. S'ils concordent mais la réponse est absurde : problème de prompt | |
| H6 | Vérifier l'accueil au début de l'enregistrement | ⚠️ Il n'y sera **pas** : il est injecté hors pipeline. Attendu, pas un bug |

**H3 est le test qui justifie tout le chantier.** Avant, un blanc de 3 s ne disait rien.
Maintenant il dit lequel des quatre maillons l'a produit.

## I. Ce que l'audit du 18/09 a changé (v1.1.0)

| # | Test | Attendu | ✅/❌ |
|---|---|---|---|
| S1 | Après deux ou trois appels, sonde → « Signature des requêtes Twilio » | Acceptées > 0, **refusées = 0** (webhooks et flux) ; console Twilio → *Monitor* : aucune erreur 11200 ni 403 | |
| S2 | 48 h plus tard : retirer `TWILIO_SIGNATURE=log`, redéployer, rappeler | Ça décroche toujours (mode `enforce`) | |
| O1 | Saisir les horaires dans l'admin (fermé le lundi), puis « une table lundi à 20h » | Refus poli **avec** une alternative ouverte ; **rien** en base | |
| O2 | « Mardi à 20h » (jour ouvert) | Acceptée | |
| N1 | Réserver (B1) | E-mail reçu : date en toutes lettres, couverts, numéro de l'appelant | |
| N2 | Laisser un message (« rappelez-moi pour un groupe ») | E-mail « message pris » reçu | |
| N3 | Modifier puis annuler (C1, C3) | Deux e-mails : modifiée, annulée | |
| P1 | Rappeler du même numéro après B1 et demander une table | Elle **propose** « au nom de Martin ? » au lieu de le demander ; un autre nom donné est celui retenu | |
| K1 | `python scripts/test_moshi_server.py --url "$MOSHI_TTS_URL" --api-key public_token` | **Refusé** ; avec la vraie clé, accepté ; sonde « Jeton du serveur de voix » verte | |
| R1 | Lancer `/opt/backups/backup-db.sh` à la main | Finit par « copie distante ok » ; `rclone ls` montre l'archive ; sonde « Sauvegarde » verte | |
| C7 | Admin → Réservations → « Annuler » | Ligne **barrée**, toujours là : l'admin n'efface plus | |

**O1 est le défaut critique de l'audit** : la disponibilité répondait toujours oui, et
« lundi 20 h » était pris un jour de fermeture. **S1 conditionne S2** : tant qu'une seule
requête est refusée en `log`, ne pas passer en `enforce` — ce serait couper la ligne.

## G. Le test qu'on oublie toujours

| # | Test | Attendu | ✅/❌ |
|---|---|---|---|
| G1 | Appeler et **ne rien dire** pendant 20 s | Elle relance, ne boucle pas, ne raccroche pas brutalement | |
| G2 | Parler **par-dessus** elle | Elle s'interrompt (INTERRUPTION=mots : seule une vraie transcription coupe) | |
| G3 | Demander quelque chose de hors sujet | Elle ramène poliment au restaurant | |
| G4 | Dire « je suis allergique aux fruits de mer » | Elle **ne garantit rien**, renvoie vers l'équipe en salle | |
| G5 | Raccrocher brutalement en plein milieu | L'appel est clôturé en base (`ended_at` non nul) | |

**G5 teste le contrôle « appels jamais clôturés » de la supervision.**
**G4 touche aux données de santé** (`docs/RGPD.md` §4).

---

## Après la recette

1. Reporter la **durée moyenne réelle** dans `docs/TARIFS.md` et refaire les marges.
2. Noter les formulations entendues qui ont mal marché → prompt système.
3. Ce qui a échoué devient un ticket, avec la phrase exacte prononcée.
