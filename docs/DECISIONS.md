# Décisions, questions en attente, remarques sans ticket

La mémoire de travail du projet (ASSISTANTE-169). La session principale le **lit au
démarrage** et le **met à jour à la fin de chaque lot**. Ce qui ne se décide pas dans le code
se décide ici, avec sa date et sa source : une question sans réponse ne se perd plus, une
remarque ne reste plus dix jours sans ticket.

- **Décisions** : ce que Helmi a tranché. Une décision ne s'efface pas ; une décision
  remplacée est barrée et renvoie à la nouvelle.
- **Questions en attente de Helmi** : posées, sans réponse. Un silence n'est pas un refus
  (Helmi, 24/10/2026) : la question reste ici jusqu'à la réponse, puis passe en décision.
- **Remarques sans ticket** : un défaut ou un risque vu en passant. Il en sort par un ticket
  Jira (on note sa clé) ou par une décision de ne rien faire.

Les conversations parallèles de Helmi sont en lecture seule : quand une décision y est
prise, elles finissent par un bloc « À reporter dans DECISIONS.md », que Helmi colle dans la
session principale (`CLAUDE.md`, « Qui décide quoi »).

## Décisions

| Date | Décision | Pourquoi | Source |
|---|---|---|---|
| 24/10/2026 | **Livraison (CD)** : image construite en CI et publiée sur GHCR privé ; préproduction sur le même serveur, acceptée même si elle coûte un peu plus ; releases groupées, pas une par déploiement. Pour l'instant, Claude déploie la production quand Helmi le demande ; le bouton d'approbation GitHub viendra avec de vrais clients. | L'image de production doit être celle qui a été testée ; préparer le circuit complet sans le brancher trop tôt. | Conversation parallèle, puis chantier « livraison » (ASSISTANTE-167, 168) |
| 24/10/2026 | **Dépôt : reste public.** Le passage en privé, c'est Helmi qui le fait, s'il le décide. | Chiffré le 24/10 sur 30 jours d'exécutions : en privé, 1 830 à 4 550 min par mois pour 2 000 gratuites (CI 1 668 min ; la sonde, cadencée toutes les 15 min, coûterait 2 880 min à elle seule si GitHub la lançait vraiment à ce rythme — il ne l'a lancée que 162 fois en 30 jours). Le bouton d'approbation n'existe en privé qu'avec Enterprise (21 $ par utilisateur et par mois) ; CodeQL n'est gratuit qu'en public. | Pop-up, session principale |
| 24/10/2026 | **Arrivée d'un client** : renvoi d'appel (`*21*`, `*61*`, `*67*`), pas de portabilité pour l'instant ; Helmane détient les numéros ; parcours automatisé du formulaire jusqu'à la ligne active, sauf l'achat du numéro (clic de Helmi) et le geste du client sur sa ligne ; vérification automatique du renvoi par un appel. | Le client garde son numéro et peut tout défaire seul ; Helmane maîtrise la ligne. | Conversation parallèle |
| 24/10/2026 | **Téléphonie** : rester sur Twilio ; plus tard, éventuellement Telnyx ou Plivo (gérés par Pipecat) ; Asterisk seulement quand le volume le justifie (minutes par mois × 0,0144 $ nettement au-dessus du coût d'un trunk SIP et de son exploitation). | 0,0144 $ la minute reçue, flux audio compris (facture Twilio de septembre 2026). | Conversation parallèle |
| 24/10/2026 | **Autonomie des agents** : bornée. Helmi garde les priorités, la production, l'argent, le client et le juridique. Une porte se retire quand les chiffres montrent qu'elle ne sert plus. | — | Conversation parallèle |
| 24/10/2026 | **Sauvegarde de la base hors du serveur** : Helmi la fera au début des essais avec un vrai client. | Hors chantier tant qu'aucun client réel n'a de données. | Conversation parallèle |
| 24/10/2026 | **Second regard sur le code** : outils d'analyse dans la CI tout de suite ; revue de sécurité en plus du relecteur sur les lots sensibles. | Le relecteur est de la même famille de modèle que le concepteur : mêmes angles morts possibles. | Conversation parallèle (lot 3 du chantier « fiabilité ») |
| 24/10/2026 | **Pas de second modèle** (via OpenRouter) en seconde lecture : ruff, Semgrep, le relecteur et `/security-review` suffisent. | Rien à dépenser ; la clé locale était de toute façon refusée. | Pop-up, session principale (ASSISTANTE-171) |
| 24/10/2026 | Tickets 168 et 169 clos : retour arrière éprouvé sur la production (`--version`, échec simulé, redéploiement). | Accord de Helmi. | Session principale |
| 24/10/2026 | Tickets 127, 128, 129, 130, 133, 136, 137, 148 et 151 clos. | qa-recette n'y avait trouvé aucun défaut. | Session principale |
| 23/10/2026 | **Seuil du solde Twilio : 5 $** (`SUPERVISION_SOLDE_TWILIO_MIN`). Sous ce seuil : alerte, e-mail aux super-admins, la recette automatique ne compose plus. | Environ deux semaines de trafic au rythme de septembre (9,40 $ facturés) ; plus qu'un passage de recette. | Pop-up, session principale (ASSISTANTE-165) |
| 12/10/2026 | **Vrais appels d'essai seulement quand le lot le demande**, pas après chaque déploiement. | Économie : un passage de recette coûte 0,72 à 0,83 $. | Session principale |
| 10/10/2026 | **Recette automatique** : 3 $ par recette, 30 $ par mois ; établissement d'essai seulement ; jamais pendant un appel réel ni la nuit ; un passage par déploiement au plus ; seulement vers nos numéros. | Garde-fous de dépense et de sécurité. | Session principale (`CLAUDE.md`, « Qui décide quoi ») |
| 09/10/2026 | **La voix s'appelle Marie**, quel que soit le moteur ; un restaurateur ne lit jamais « GPT-Live » ni « OpenAI ». | Nom commercial. | Session principale |

## Questions en attente de Helmi

| Posée le | Question | Ce qui en dépend |
|---|---|---|
| 13/10/2026 | Afficher les **coûts fixes** (location des numéros, TVA) sur « Santé & coûts » ? Aujourd'hui seulement dans `docs/TARIFS.md`. | Un lot de page, sans urgence. |
| 13/10/2026 | **Recharge automatique** du compte Twilio (montant, seuil) ? | Éviter une nouvelle suspension ; l'alerte de 5 $ existe déjà. |
| 13/10/2026 | **Numéro de TVA** à déclarer chez Twilio ? | La TVA de 20 % facturée en septembre (1,57 $). |
| avant le 13/10/2026 | **Accès partenaire Uniiti** pour le premier client. | Faire essayer le premier client. |
| avant le 13/10/2026 | **Horaires du premier client** à saisir. | Sans eux, l'assistante ne refuse aucun créneau. |
| 24/10/2026 | Twilio : un seul bundle « Business » au nom d'Helmane suffit-il pour tous les numéros (le bundle actuel est « Individual ») ? Un numéro géographique doit-il correspondre à l'adresse du dossier ? | L'achat des numéros des clients. À vérifier chez Twilio. |
| 24/10/2026 | Chantier « fiabilité », lot 1 : heure de la tournée quotidienne, plafond mensuel si un modèle intervient. | Sera posé en pop-up avec le lot 1. |

## Remarques sans ticket

| Vue le | Remarque | Suite |
|---|---|---|
| 13/10/2026 | Un appel qui tombe en panne en cours de route a eu un flux audio que son chiffrage de secours (`conclure_secours`) ne compte pas : 0,0044 $ par minute, cas rare. | Aucune ; écrit dans le code et au `CHANGELOG.md`. |
| 13/10/2026 | Le tarif américain (0,0085 $ la minute) n'est pas distingué : le numéro américain ne sert plus qu'au Banc d'essai. | Aucune tant que c'est vrai. |
| 24/10/2026 | « Rappelez-moi » n'est pas freiné par le solde Twilio. Avec un compte suspendu, Twilio refuse de toute façon. | À décider. |
| 24/10/2026 | Les e-mails d'épisode (solde Twilio, crédit OpenAI) sont marqués envoyés avant l'envoi : une panne de la messagerie à ce moment-là perd l'e-mail. | Choix assumé ; la supervision reste en alerte. |
| 12/10/2026 | T19 : 5 requêtes au cerveau sur 24 ont été redemandées (1 sur 21 avant). | À surveiller quand les appels reprendront. |
| 12/10/2026 | Le Banc d'essai n'a plus de numéro de secours (retiré le 12/10) : S3 à S5 ne se jouent plus sur lui, et la supervision signale « 1 établissement sans numéro de secours ». | Voulu ; à revoir si S3-S5 doivent redevenir automatiques. |
| 24/10/2026 | La clé OpenRouter du `.env` local est refusée par OpenRouter (401 « User not found ») : révoquée ou d'un compte supprimé. Rien ne s'en sert en local hormis les bancs. | À remplacer ou retirer par Helmi. |
| 24/10/2026 | Les règles de Semgrep viennent de son registre et peuvent changer sans commit chez nous : une nouvelle règle peut rougir la CI d'un lot sans rapport. Les recopier dans le dépôt public serait les redistribuer, ce que leur licence (Semgrep Rules License v1.0) encadre : non fait. | Voulu (ASSISTANTE-171) ; une alerte nouvelle se traite comme les autres. |
| 24/10/2026 | La CI accorde `packages: write` au job `image` aussi sur les branches, où rien n'est publié. | Chantier « livraison », étape 7 (actions épinglées). |
| 24/10/2026 | Le retour arrière ne voit pas une aggravation sur un contrôle déjà en panne, ni un passage « ok » → « attention », ni ce qui demande des appels. | Écrit dans `docs/DEPLOY.md` ; voulu. |
| avant le 13/10/2026 | Deux dossiers de travail d'agents restent sur le disque (`.claude/worktrees/agent-a08c7fa725acb0bd0`, `agent-a63db2a8b6ef0416d`) : suppression refusée, des caches écrits par Docker appartiennent à root. Vérifié le 24/10 : chacun de leurs fichiers est déjà dans git, rien ne se perd. | Commande `sudo` donnée à Helmi (ASSISTANTE-170) ; à retirer d'ici quand c'est fait. |
