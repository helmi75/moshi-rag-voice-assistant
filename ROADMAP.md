# Roadmap

**Objectif** : une assistante qui décroche à la place de l'équipe des restaurants
débordés d'appels — réserver, renseigner, prendre un message — vendue par abonnement.

La source de vérité du travail est la liste des **issues GitHub** ; ce fichier en donne
l'ordre et l'état au 19/09/2026. L'historique de ce qui est livré est dans
[CHANGELOG.md](CHANGELOG.md).

## Livré dans le code, issue encore ouverte

À vérifier puis fermer sur GitHub :

| Issue | Sujet | Où c'est |
|---|---|---|
| #22 | Obligations RGPD | `app/rgpd.py`, [docs/RGPD.md](docs/RGPD.md) |
| #29 | Grille tarifaire | `app/plans.py`, [docs/TARIFS.md](docs/TARIFS.md) |
| #31 | Compter les appels, plafond de la formule | `app/quotas.py` |
| #33 | Modifier et annuler par téléphone | `llm.run_tool`, `app/reservations.py` |
| #40 | Plafonner les GPU simultanés | `MODAL_MAX_CONTAINERS` (défaut 4) |
| #88 | Enregistrer et journaliser les appels | `voice/enregistrement.py`, `voice/journal.py` |
| #28 | Kyutai STT par défaut | **abandonné** : dérive vers l'anglais sur du μ-law 8 kHz bruité ; Deepgram reste |

## 1. Avant le premier client

| Quoi | Pourquoi |
|---|---|
| Déployer la branche de l'audit (v1.1.0 du CHANGELOG), puis la [recette](docs/RECETTE.md) | signature Twilio, horaires, e-mails, clé Modal, copie distante des sauvegardes : rien de tout ça n'a encore entendu un vrai appel |
| #30 Numéro de démonstration public (numéro FR, région EU) | un prospect doit pouvoir appeler avant de signer |
| #32 Un restaurant pilote, deux semaines de vrais appels | seul juge de la qualité perçue |

## 2. Ensuite, par valeur

| Quoi | Pourquoi |
|---|---|
| #26 GPU chaud aux heures de service seulement | le premier appel après une pause attend le réveil du GPU (55-70 s couverts par l'accueil et la musique) ; à trancher au banc et à l'oreille |
| Qualité mesurée en continu | banc de conversation nocturne en CI avec un seuil ; score d'interaction par appel tiré du journal de bord ; A/B du délai de fin de tour |
| #36 Facturation Stripe | le plafond compte déjà (`quotas`) ; il reste à encaisser |
| Compte client multi-établissements | la formule Maison (5 établissements) n'est pas vendable sans lui : `quotas.groupement_manquant` |
| #34 SMS de confirmation au client | l'e-mail au restaurateur est fait ; le client, lui, n'a qu'une confirmation orale |
| #35 Transfert vers un humain | demande explicite, urgence |
| #37 Achat du numéro en un clic depuis l'admin | onboarding sans intervention |

## 3. Plus tard, sur déclencheur

| Quoi | Déclencheur |
|---|---|
| #38 PostgreSQL | le jour où l'API doit tourner en plusieurs exemplaires ; SQLite (WAL, hors boucle) suffit pour une instance |
| Documents volumineux (PDF, site) → embeddings | une fiche qui dépasse le plafond de 12 000 caractères |
| Intégrations TheFork, Zenchef, agenda | des clients qui les utilisent déjà |
| Verticale médicale | produit prouvé sur les restaurants ; exige un hébergement HDS |

## Principes

1. **Payer à l'usage** : GPU à la demande (scale-to-zero), APIs ; pas de coût fixe sans trafic.
2. **Le différenciateur est le cerveau métier** (fiche, outils, refus côté serveur), pas
   le pipeline audio : `llm.py` ignore le transport.
3. **Aucun chiffre inventé, aucun feu vert décoratif** : ce qui est affiché est mesuré,
   et chaque garde-fou a un test qui rougit quand on le retire.
4. **Vendre avant de sur-construire** : chaque étape a un jalon commercial.
