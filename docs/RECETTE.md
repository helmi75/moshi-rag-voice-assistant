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
| A2 | Écouter le tout début | L'accueil du restaurant, **puis** « Je suis l'assistante vocale, cet appel est enregistré. », puis « Je vous écoute. » : ~5 s en tout, sans « un instant s'il vous plaît » (SCRUM-107) | |
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
| C4 | Admin → Réservations, vue **Jour** de la réservation | Carte **barrée**, pastille « Annulée », date d'annulation ; elle ne compte plus dans les couverts du jour | |
| C5 | Rappeler et redemander le même créneau | Il est **de nouveau libre** | |
| C6 | Appeler en masqué et demander une annulation | Elle prend le message, **n'annule rien**, n'invente pas | |
| C8 | Réserver, puis dire « mon nom a été mal noté, c'est K, I, K, A, O » (ASSISTANTE-126) | Elle corrige le nom. Dans l'admin : **une seule** réservation, au bon nom ; un e-mail « Réservation modifiée », pas de seconde « Nouvelle réservation » | |

**C5 est le piège silencieux** : si le créneau est refusé, `ACTIVES` ne s'applique pas
partout et une table annulée compte encore.

**C1 est le vrai test de langage.** Écoute comment elle formule quand il y a plusieurs
réservations, et **note ses mots** : c'est ce qui permettra d'affiner le prompt.

## D. Ce que ça coûte vraiment — la question qui décide de la grille

| # | Test | Attendu | ✅/❌ |
|---|---|---|---|
| D1 | Faire **10 appels réalistes** (durée normale, pas d'exploration) | | |
| D2 | Admin → Santé & coûts → durée moyenne et coût moyen | Le coût d'une minute : `docs/TARIFS.md` retient 2,5 c€ (relevé du 28/09, sur des appels de test) | |
| D3 | Comparer à la borne haute de `docs/TARIFS.md` | Si la minute coûte plus de 5 c€ : la grille est à revoir | |
| D4 | Blanc médian | Comparer à 1,16 s (mesuré le 30/07) | |

## E. Forfait et facturation — #31, ASSISTANTE-120 (grille à la minute)

| # | Test | Attendu | ✅/❌ |
|---|---|---|---|
| E1 | Admin → fiche établissement | Le sélecteur de **Formule** apparaît (super-admin seulement) | |
| E2 | Se connecter en restaurateur | Le sélecteur **n'apparaît pas** | |
| E3 | Salle de contrôle | Carte « Forfait », compteur du **mois calendaire**, en **minutes** (« 12 minutes d'appel ce mois-ci sur 250 minutes incluses, en 5 appel(s) » ; sous la minute, en secondes) | |
| E4 | Éprouver le forfait sans passer 250 minutes : attribuer la formule à un établissement de test, ou insérer en base des lignes `calls` du mois en cours avec leur `duration_seconds` | Alerte à 80 %, puis « Forfait dépassé » avec les minutes et le montant — **et la ligne continue de répondre** | |
| E5 | Passer un appel d'une minute environ, raccrocher, recharger la salle de contrôle | Le compteur a avancé de la durée de l'appel, **à la seconde** (pas d'une minute entière par appel) | |
| E6 | Fiche établissement → Formule | Quatre formules, en minutes : « Liberté — sans abonnement, 0.45 € la minute », « Essentiel — 89 €/mois, 250 minutes »… | |
| E7 | Attribuer **Liberté** à un établissement de test, puis ouvrir sa salle de contrôle | « Formule Liberté · Sans abonnement », les minutes et le montant du mois ; **ni barre, ni plafond, ni alerte** | |
| E8 | Vue du parc, sur un ordinateur puis sur un téléphone | « min ce mois-ci » : `12/250` avec un forfait, `12` sans forfait ; les quatre chiffres sont **sous** le nom de l'établissement, rien ne se chevauche | |
| E9 | Un appel renvoyé vers le restaurant pendant une panne (recette S) | Il **n'ajoute aucune minute** au forfait | |

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
| O3 | « Ce que l'IA sait » : enregistrer les horaires sans aucune plage, cases « Fermé » cochées ou non (ASSISTANTE-130) | Refus : « Aucune plage d'ouverture saisie : rien n'a été enregistré » ; les horaires d'avant sont toujours là | |
| N1 | Réserver (B1) | E-mail reçu : date en toutes lettres, couverts, numéro de l'appelant | |
| N2 | Laisser un message (« rappelez-moi pour un groupe ») | E-mail « message pris » reçu | |
| N3 | Modifier puis annuler (C1, C3) | Deux e-mails : modifiée, annulée | |
| N4 | Après quelques jours d'appels : chercher « tentative » et « non envoyée » dans le journal du conteneur | Des e-mails partis « à la tentative 2 » ; aucun « non envoyée » pour une poignée de main refusée | |
| P1 | Rappeler du même numéro après B1 et demander une table | Elle **propose** « au nom de Martin ? » au lieu de le demander ; un autre nom donné est celui retenu | |
| K1 | `python scripts/test_moshi_server.py --url "$MOSHI_TTS_URL" --api-key public_token` | **Refusé** ; avec la vraie clé, accepté ; sonde « Jeton du serveur de voix » verte | |
| R1 | Lancer `/opt/backups/backup-db.sh` à la main | Finit par « copie distante ok » ; `rclone ls` montre l'archive ; sonde « Sauvegarde » verte | |
| C7 | Admin → Réservations → vue Jour → « Annuler » | Carte **barrée**, toujours là : l'admin n'efface plus | |

**O1 est le défaut critique de l'audit** : la disponibilité répondait toujours oui, et
« lundi 20 h » était pris un jour de fermeture. **S1 conditionne S2** : tant qu'une seule
requête est refusée en `log`, ne pas passer en `enforce` — ce serait couper la ligne.

## R. resOS en bac à sable — SCRUM-84 à 87 et 93, sans clé ni abonnement

Préparer : fiche de l'établissement → **Carnet de réservations : resOS — bac à sable**, puis
ouvrir la page **Carnet resOS** du menu. Elle se rafraîchit toute seule toutes les 5 s :
la laisser ouverte pendant les appels.

| # | Test | Attendu | ✅/❌ |
|---|---|---|---|
| R1 | Appeler, demander une table pour demain 20 h | Elle dit « demande transmise au restaurant », **jamais « confirmé »** ; la ligne apparaît en « À valider », source « Téléphone (assistante) » | |
| R2 | Sur la page : **Valider** | La ligne passe en « Validée » | |
| R3 | Rappeler du même numéro : « je voudrais passer à 4 personnes » | Elle retrouve la réservation ; la ligne affiche 4 couverts | |
| R4 | Rappeler : « je voudrais annuler » | La ligne passe en « Annulée » | |
| R5 | Réglages : 0 table par créneau. Appeler | « Complet » : elle **propose d'autres horaires** (ou un autre jour), n'enregistre rien | |
| R6 | Réglages : cocher le jour de demain dans « Jours fermés ». Appeler pour demain | Elle refuse ce jour-là ; « Ce que l'IA sait » l'affiche fermé, **en lecture seule** (horaires lus dans resOS) | |
| R7 | Bouton **« resOS en panne »** (bandeau rouge en haut de la page). Appeler et demander une table | Elle **n'annonce rien** comme enregistré et prend un message ; le journal de la page montre les 503 en rouge ; e-mail « resOS ne répond pas » (si le SMTP est réglé) ; `/supervision` → « Carnet resOS » en attention (bac à sable) | |
| R8 | Pendant la même panne, redemander dans le même appel | Réponse immédiate, sans nouveau blanc de 4 s (coupe-circuit) | |
| R9 | Bouton **« resOS fonctionne »** (confirmation verte), rappeler | Tout refonctionne tout de suite | |
| R10 | Bouton **« resOS lent »**. Appeler | Même comportement que R7, après 4 s d'attente au plus. Sans retour à « resOS fonctionne », arrêt automatique au bout de 30 min | |

**R1 est le test qui compte** : une réservation que l'assistante annonce et que le carnet ne
contient pas est la seule faute impardonnable de toute l'intégration.

## V. Les voix Mistral (Voxtral) — SCRUM-94, SCRUM-99

Prérequis : `MISTRAL_API_KEY` posée sur le serveur (docs/VOXTRAL.md).

| # | Faire | Attendu | OK ? |
|---|---|---|---|
| V1 | « Voix & accueil » : parcourir la liste | 30 voix regroupées : Marie (Français), Jane, Oliver, Paul ; tons en français (« Paul · joyeux ») | |
| V2 | Sélectionner une voix dans la liste, puis lecture | Le lecteur fait entendre CETTE voix (qualité téléphone), avant tout enregistrement | |
| V3 | Choisir une voix, enregistrer | « Actuellement : … » ; l'accueil regénéré s'écoute en quelques secondes | |
| V4 | Appeler, **à n'importe quelle heure** | L'accueil, puis « Je vous écoute. » tout de suite : **pas de musique d'attente** | |
| V5 | Réserver une table | Toute la conversation dans la voix choisie, relances et « pardon » compris | |
| V6 | « Santé & coûts » → carte « Répartition du coût » : additionner les montants des lignes | On retombe sur le total écrit sous le titre de la carte. La ligne « Voix Moshi (historique) » ne compte que les appels d'avant la bascule : elle baisse de jour en jour | |
| V6 bis | Appels → ouvrir un appel **récent** d'environ 2 min (voix Mistral) | Son coût, écrit à côté de sa durée, est d'environ 0,05 $. Ne pas le comparer au « coût moyen par appel » : cette moyenne porte sur 30 jours et compte encore les anciens appels Moshi | |
| V7 | Un établissement qui n'a rien choisi | Il parle avec la voix par défaut du parc (Marie enthousiaste depuis le 03/10/2026) | |

## P. L'admin du restaurateur — Sprint 2 « Admin pro » (SCRUM-106 à 113, ASSISTANTE-114 à 116)

À faire avec un **compte restaurateur**, puis une fois en super-admin.

| # | Faire | Attendu | OK ? |
|---|---|---|---|
| P1 | « Voix & accueil » : changer de voix, enregistrer, écouter l'aperçu | L'aperçu parle dans la **nouvelle** voix et dit le texte affiché sous le lecteur, mention comprise (SCRUM-106) | |
| P2 | Appels → ouvrir un appel (restaurateur) | Lecteur de l'enregistrement et transcription ; **pas** de lien « Diagnostic » (SCRUM-108) | |
| P3 | Vue du parc / Appels : graphiques « par jour » | Une valeur au-dessus de chaque barre non nulle, lisible sur téléphone (SCRUM-113) | |
| P4 | « Ce que l'IA sait » | Les horaires en tête (plus de menu « Horaires d'ouverture »), puis les fiches (SCRUM-110) | |
| P5 | Une fiche → **Modifier**, changer une ligne, **Enregistrer** | « Fiche … enregistrée. » ; au prochain appel, l'assistante cite la nouvelle information (SCRUM-111) | |
| P6 | **Ajouter une fiche** « Accès », puis la **Supprimer** | Elle apparaît, puis disparaît ; la jauge de caractères suit | |
| P7 | Deux onglets sur « Ce que l'IA sait » : modifier dans l'un, puis dans l'autre | Le second est **refusé** (« modifiée entre-temps ») : rien n'est écrasé | |
| P8 | « Fiche établissement » | Seulement l'identité, la ligne et l'e-mail ; plus de grand champ de texte | |
| P9 | Réservations (ordinateur) | Le **mois** : réservations et couverts par jour, lundi grisé si fermé ; un clic sur un jour ouvre le jour (SCRUM-112) | |
| P10 | Réservations → Semaine | Chaque réservation à son heure : heure, couverts, nom ; annulées barrées | |
| P11 | Réservations → Jour → Modifier l'heure d'une réservation | La page se recharge, la réservation est à sa nouvelle heure | |
| P12 | Réservations **sur téléphone** | S'ouvre sur la vue du **jour** | |
| P13 | Établissement en resOS (bac à sable) → Réservations | Les demandes apparaissent « À valider » ; pas de bouton Modifier : resOS fait foi | |
| P14 | Super-admin → Réservations → « Tous les établissements », puis un seul | Tout le parc (le nom sur chaque carte), puis ce seul établissement | |
| P15 | Barre latérale (ou barre du haut sur téléphone) : **Sombre**, puis **Clair**, puis **Comme l'appareil** | La page bascule aussitôt, sans rechargement ; le choix tient d'une page à l'autre et après déconnexion (ASSISTANTE-114) | |
| P16 | Thème **Sombre** : parcourir Salle de contrôle, Appels, Réservations, une fiche | Tout est lisible : graphiques, calendrier, cartes, encadrés | |
| P17 | Salle de contrôle / Appels : passer la souris sur les barres « Appels par jour » et « Réservations par jour » | Une bulle donne la date en toutes lettres et le nombre (« 4 appels · Samedi 26 septembre 2026 ») ; un jour à zéro aussi. Au doigt : un toucher (ASSISTANTE-115) | |
| P18 | Réservations → **Jour** → cliquer une réservation prise au téléphone | Sa fiche : la réservation, le client (numéro cliquable, ses autres réservations, ses appels) et la conversation, enregistrement et transcription (ASSISTANTE-116) | |
| P19 | Même chose sur une réservation prise par SMS (sans appel) ; puis, sur une carte : les boutons Modifier / Annuler, et sélectionner le numéro à la souris | « Aucun appel rattaché », sans supposer d'où elle vient ; les boutons agissent sans ouvrir la fiche ; le numéro se sélectionne et se copie | |
| P20 | Sur téléphone : toucher une barre d'un graphique, lever le doigt, toucher la même barre | La bulle reste affichée après la levée du doigt, et revient au second toucher | |
| P21 | Passer un appel, noter l'heure à sa montre, puis ouvrir Appels | L'appel est à **l'heure de Paris** (la même qu'à la montre), dans le journal, la salle de contrôle et la fiche de l'appel | |
| P22 | Ouvrir l'admin en sombre, puis en clair (les trois pastilles en bas de la barre latérale) | Les couleurs du site : fond brun nuit et orange en sombre, crème en clair ; aucun bouton bleu ; les graphiques en orange (appels) et bleu canard (réservations) | |
| P23 | Sur un téléphone : toucher le bouton aux trois barres, en haut à droite | Le menu se déroule (navigation, thème, compte) ; le bouton devient une croix ; un toucher sur la page ou sur la croix le referme | |
| P24 | Sur un téléphone, menu fermé | Une seule ligne en haut : la marque et le bouton ; le contenu commence tout de suite dessous | |
| P25 | Salle de contrôle d'un établissement qui a plus de six tables à venir | « Prochaines réservations » commence par celles du jour, la plus proche en premier ; une longue note est sous la date, terminée par « … » ; le soir, les tables du midi déjà servies n'y sont plus | |
| P26 | Sur un téléphone : Appels | Les six filtres tiennent dans l'écran, sur deux lignes ; la page ne glisse pas sur le côté | |
| P27 | Sur un téléphone, en super-admin : Enseignes | Chaque nom sur une ligne, ses six actions dessous ; « Comptes » et « Supprimer » se touchent du doigt | |
| P28 | Sur un téléphone : Salle de contrôle, les graphiques | Titres et chiffres lisibles ; « Appels par jour » glisse du doigt et s'ouvre sur aujourd'hui ; les créneaux du jour tiennent entiers | |
| P29 | Vue du parc, un établissement dont le seul appel du mois a duré moins de 30 secondes | « <1/250 », jamais « 0/250 » | |
| P30 | Fiche établissement, sur un téléphone | Les listes déroulantes ont leur flèche ; la formule s'affiche entière (« Service · 149 €/mois · 600 min ») | |
| P31 | Un montant, n'importe où dans l'admin | Une virgule : « 3,89 $ », « 6,42 € » | |
| P32 | Appels, sur un téléphone : toucher un appel au milieu de la liste (ASSISTANTE-136) | Sa fiche s'ouvre juste sous la ligne touchée, l'écran s'y cale ; sur ordinateur, la fiche reste à droite | |

## S. En cas de panne : le renvoi vers le restaurant (ASSISTANTE-118)

À faire **hors service** : pendant un essai, l'assistante ne prend aucun appel de
l'établissement. Il faut deux téléphones : celui qui appelle, et celui du numéro de secours.

| # | Faire | Attendu | OK ? |
|---|---|---|---|
| S1 | « Fiche établissement » → **Numéro de secours** : saisir la ligne de l'assistante | Refusé : « l'appel renvoyé reviendrait ici » | |
| S2 | Saisir le fixe du restaurant (ou le portable du gérant), enregistrer | Enregistré ; l'alerte « Pas de numéro de secours » disparaît de la salle de contrôle | |
| S3 | Super-admin → *Essayer le renvoi*, puis appeler la ligne **pendant les horaires d'ouverture** | « Un instant, je vous mets en relation avec le restaurant », puis le téléphone de secours sonne et affiche **le numéro de l'appelant** | |
| S4 | Décrocher, parler, raccrocher. Ouvrir Appels | L'appel est « Renvoyé au restaurant », avec la durée de la communication ; son coût compte la téléphonie des deux lignes | |
| S5 | Relancer l'essai, appeler, **ne pas décrocher** | Après 15 s de sonnerie : « Notre assistante ne peut pas vous répondre… », bip. Laisser un message | |
| S6 | Attendre une minute, ouvrir Appels | « Message vocal », un rappel à traiter, et le message s'écoute dans la fiche de l'appel ; un e-mail « Message vocal à rappeler » est arrivé | |
| S7 | Relancer l'essai **hors horaires** (ou retirer le numéro de secours), appeler | Le répondeur tout de suite, sans faire sonner personne | |
| S8 | *Arrêter l'essai*, appeler | L'assistante répond normalement | |
| S9 | Après l'essai : « Santé & coûts » | Le contrôle « Appels passés en secours » ne compte pas les essais | |
| S10 | Appeler la ligne et raccrocher dès la première sonnerie, puis rappeler aussitôt (ASSISTANTE-127) | Le second appel est servi par l'assistante, pas par le répondeur ; « Appels passés en secours » ne bouge pas | |

## W. Le site et « Rappelez-moi » (ASSISTANTE-119)

Chaque rappel est un vrai appel sortant, facturé : W5 à W9 suffisent, inutile de les
répéter. Il faut un portable français de métropole.

| # | Faire | Attendu | OK ? |
|---|---|---|---|
| W1 | Ouvrir `https://helmane.fr` sur un ordinateur | Arrive sur `https://app.helmane.fr/` ; la page s'affiche avec ses polices et ses dessins ; dans la console du navigateur (F12), aucune ligne rouge « Content Security Policy » | |
| W2 | La même page sur un téléphone, jusqu'en bas | Rien ne déborde, rien n'est illisible ; la page est **sombre**, quel que soit le réglage de l'appareil ; en haut, deux boutons : « Espace client » et « Être rappelé » ; les prix sont ceux de la grille : 89 € pour 250 minutes, 149 € pour 600, 349 € pour 1 500, la minute en plus à 0,35 / 0,25 / 0,20 € ; sous chaque forfait, « soit environ 70 / 170 / 430 appels » ; sous les trois cartes, le bandeau Liberté à 0,45 € la minute ; en bas, « Prix hors taxes » et la durée sur laquelle les appels sont calculés (3 min 30) | |
| W3 | Saisir `06 12 34`, puis *Rappelez-moi* | « Ce numéro semble incomplet » ; rien ne part | |
| W4 | Saisir un numéro en `08` | « Ce numéro n'est pas un numéro français de métropole » | |
| W5 | Entre 8 h et minuit, saisir son portable | « Votre téléphone va sonner » ; il sonne en moins de dix secondes et affiche la ligne de démonstration | |
| W6 | Décrocher | Sans blanc : « Bonjour, c'est Marie, l'assistante vocale d'Helmane. Vous avez demandé à être rappelé… », puis la mention d'enregistrement | |
| W7 | Réserver une table, puis demander « c'est quoi Helmane ? » | La table est enregistrée dans le carnet de l'établissement de démonstration ; elle répond qu'elle est là pour la démonstration et renvoie au site, sans rien inventer | |
| W8 | Admin → Appels | L'appel porte « Rappel du site » ; sa téléphonie est au tarif sortant (0,0404 $ par minute entamée vers un portable) | |
| W9 | Redemander un rappel deux fois avec le même numéro | Le deuxième sonne ; le troisième est refusé : « Ce numéro a déjà demandé plusieurs rappels en 24 heures » | |
| W10 | Ne pas décrocher (à faire au deuxième rappel de W9) | Le téléphone sonne 25 s puis s'arrête ; aucun appel au journal | |
| W11 | Entre minuit et 8 h, saisir son portable (ASSISTANTE-125) | « C'est noté. Marie ne rappelle pas à cette heure-ci : nous vous rappelons à partir de 8 h » ; le téléphone **ne sonne pas** ; un e-mail « À rappeler — demande reçue sur le site hors plage » arrive dans la boîte de `ADMIN_EMAIL`, avec le numéro | |
| W12 | Boîte de `ADMIN_EMAIL` | Un e-mail « Demande de rappel depuis le site » par rappel passé | |
| W13 | **Une fois l'éditeur renseigné** (`site.EDITEUR`) : pied de page → *Mentions légales*. Avant : ni lien, ni page | La page s'ouvre ; l'hébergeur est Hostinger ; les durées annoncées (30 jours pour la demande, 90 pour le numéro) ; l'identité de l'éditeur est complète **une fois fournie** | |
| W14 | Relire le haut de la page et le bas des formules | Plus de « 14 jours d'essai », de « sans engagement » sous les formules, ni de « tarif fondateur » ; « Sans engagement. » n'est écrit qu'une fois, sous le formulaire du haut | |
| W15 | Se faire rappeler | Marie présente « Le Bouchon Doré », avec la voix **Marie enthousiaste** ; elle ne cite ni le Fouquet's ni les Champs-Élysées | |
| W16 | Regarder l'onglet du navigateur et le haut de la page, sur le site puis dans l'espace client (ASSISTANTE-124) | Le logo est une **toque de chef** orange avec trois barres de voix dedans, plus un combiné de téléphone ; il reste net en tout petit dans l'onglet ; dans l'espace client, il est lisible en clair comme en sombre | |

## X. Les appels traités en Irlande (ASSISTANTE-123)

Marche à suivre : `docs/TWILIO_SETUP.md` § 8. À faire **hors service**. Le gain attendu
(environ 65 ms par réplique, mesuré sur le réseau) ne se lit pas dans nos fiches d'appel.

| # | Faire | Attendu | OK ? |
|---|---|---|---|
| X1 | Jeton irlandais posé et déployé, **avant** toute bascule : appeler la ligne | L'assistante répond comme avant ; « Signature des requêtes Twilio » : aucune refusée | |
| X2 | `python3 scripts/twilio_region.py +33…` | « appels traités en : us1 », et les réglages du numéro lisibles dans les deux régions | |
| X3 | `… --preparer ie1`, puis relire l'état | Les mêmes webhooks en `ie1` qu'en `us1` | |
| X4 | `… --basculer ie1`, attendre cinq minutes, appeler | L'assistante répond ; le compteur `ie1` de la signature **augmente** après l'appel (la sonde du script en avait déjà compté une) ; l'appel est au journal de la console Twilio, région Ireland | |
| X5 | Poser `TWILIO_REGION=ie1`, redéployer, prendre une réservation complète et raccrocher | Tout se passe comme avant ; à l'oreille, les réponses ne sont pas plus lentes | |
| X6 | Refaire S5 et S6 (message vocal pendant un essai de renvoi) | Le message s'écoute dans la fiche de l'appel | |
| X7 | « Rappelez-moi » sur le site | Le téléphone sonne : l'Irlande a le droit d'appeler la France | |
| X8 | Envoyer un SMS à la ligne | L'assistante répond (les SMS restent signés par `us1`) | |
| X9 | Le lendemain : « Santé & coûts » | « Alertes Twilio » relevées sans erreur ; le coût Twilio de la facture au tarif attendu | |
| X10 | Si quelque chose cloche : `… --basculer us1` | L'assistante répond de nouveau, sans redéploiement | |

## T. GPT-Live, la voix-à-voix d'OpenAI (`docs/TEMPS_REEL.md`)

Sur un établissement dont le moteur est GPT-Live (Admin → Enseignes → Fiche → « Moteur de
l'appel »). Chaque minute coûte 0,05 $ à OpenAI en plus de Twilio. Faire les mêmes appels
sur un établissement resté sur la chaîne classique : c'est la comparaison qui compte.

| # | Faire | Attendu | OK ? |
|---|---|---|---|
| T1 | Écouter les extraits de `local/essai-gpt-live/` | Une voix retenue pour son français ; elle est posée dans `GPT_LIVE_VOIX` | |
| T2 | Appeler l'établissement à l'essai | L'accueil de l'établissement, mention d'information comprise, en moins de trois secondes ; au journal du conteneur : « [gpt-live] appel … » | |
| T3 | Réserver une table | Une phrase d'attente (« Je vérifie… »), puis la confirmation ; la table est dans le carnet, avec le numéro de l'appelant | |
| T4 | Lui couper la parole au milieu d'une phrase | Elle se tait aussitôt et écoute | |
| T5 | Hésiter : « Alors… ce serait pour… euh… samedi » | Elle attend la fin, sans répondre dans le blanc | |
| T6 | Demander un jour de fermeture | Refus, et une autre proposition : le refus vient du serveur | |
| T7 | Parler anglais | Elle répond en anglais | |
| T8 | « Non merci, au revoir » | Elle prend congé, et la ligne est raccrochée deux secondes après | |
| T9 | Admin → Appels → cet appel | Les deux côtés de la conversation, l'enregistrement, et le coût : voix = durée × 0,05 $, transcription à zéro, cerveau = les jetons de Gemini | |
| T10 | Appeler un établissement hors essai | Marie, comme d'habitude | |
| T11 | Dix appels de chaque côté, puis « Santé & coûts » | Le blanc médian et le coût par minute des deux, côte à côte : c'est le verdict de l'essai | |
| T12 | Admin → Appels → réécouter un appel GPT-Live passé après le 04/10/2026 au soir | Les deux voix se répondent comme au téléphone : elle ne parle jamais avant la fin de la question | |
| T13 | Fiche d'un établissement → « Moteur de l'appel » → « Chaîne classique seule », enregistrer, appeler | Marie, comme avant GPT-Live ; dans Admin → Appels, l'appel porte « chaîne classique » | |
| T14 | Remettre « GPT-Live · secours classique », enregistrer, appeler | GPT-Live répond, sans redéploiement ; l'appel porte « GPT-Live » ; Enseignes affiche « Moteur : GPT-Live » | |
| T15 | Après des appels sur les deux moteurs : « Santé & coûts » → « Coût par moteur » | Une ligne par moteur : appels, minutes, coût, coût à la minute ; « Voix GPT-Live (OpenAI) » a sa ligne dans la répartition | |
| T16 | Se connecter en restaurateur, ouvrir sa fiche | Pas de « Moteur de l'appel » ; ni la fiche ni ses appels ne nomment GPT-Live | |
| T17 | Trois réservations de suite sur GPT-Live, en confirmant chaque récapitulatif par un simple « oui » ; puis Admin → Appels et Réservations | Chaque « c'est enregistré » a sa table au carnet. Si un appel porte « À vérifier », sa fiche cite la phrase dite, un e-mail « À vérifier » est arrivé, et son résumé ne dit pas « a réservé » (ASSISTANTE-137) | |
| T18 | Après un appel « À vérifier » : `docker compose exec api` puis lire `delegations` dans le journal de l'appel | On lit ce que le cerveau a rendu à chaque travail confié : c'est ce qui dit lequel des deux modèles a annoncé à tort | |

## L. Écouter sans faire répéter — appels 183 à 201 (27/09/2026)

| # | Faire | Attendu | OK ? |
|---|---|---|---|
| L1 | À chaque question, répondre **tout de suite et court** (« oui », « 20 heures ») | Jamais « Vous êtes toujours là ? » après une réponse. Au pire « Pardon, je n'ai pas bien entendu », dans les 3 s | |
| L2 | Rester vraiment silencieux après une question | « Vous êtes toujours là ? » au bout de 8 s, comme avant | |
| L3 | Donner jour, heure et nombre ; complet ; « disons vendredi » | Elle garde l'heure et le nombre : « Vendredi, toujours vingt heures pour deux ? » | |
| L4 | Quand elle dit « je vérifie », se taire | La réponse arrive sans relance de votre part | |
| L5 | **Numéro masqué** (#31# devant le numéro), demander à laisser un message | Elle demande « À quel numéro peut-on vous joindre ? » AVANT de transmettre, relit le numéro ; l'e-mail et la fiche de l'appel l'affichent, marqué « donné par l'appelant, appel masqué » | |
| L6 | Numéro masqué, dicter un numéro incomplet | Elle fait redire le numéro en entier | |
| L7 | Fiche de l'appel → journal de bord | Compteurs `paroles_rattrapees`, `paroles_perdues`, `annonces_relancees` : ce que les filets ont fait | |

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
