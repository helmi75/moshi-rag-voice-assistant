# Données personnelles — registre et obligations (#22)

> **Base de travail, pas un avis juridique.** Ce document décrit exactement ce que le
> logiciel fait des données ; les qualifications (rôles, bases légales, durées) sont des
> propositions à faire valider avant de signer un premier restaurant.
>
> Dernière vérification technique : **30/08/2026**.

## 1. Qui fait quoi

| Rôle | Qui | Sur quoi |
|---|---|---|
| **Responsable de traitement** | Le restaurant client | Les données de SES clients : appelants, réservations |
| **Sous-traitant** | Helmane | Traite ces données pour le compte du restaurant, sur ses instructions |
| **Responsable de traitement** | Helmane | Les comptes de l'admin (`users`) : ses propres clients |

➡️ **Conséquence directe** : un **contrat de sous-traitance** (article 28) doit être signé
avec chaque restaurant **avant** le premier appel. Sans lui, le restaurateur est en
infraction et Helmane aussi. C'est le seul point de ce document qui bloque une signature.

## 2. Registre des traitements

| Traitement | Données | Base légale proposée | Durée |
|---|---|---|---|
| Prise de réservation par téléphone | Nom, téléphone, date, heure, couverts, demandes particulières | Exécution du contrat entre le restaurant et son client | **365 j** après la date de réservation |
| Journal des appels (qualité, facturation) | Numéro appelant, horodatage, durée, coût | Intérêt légitime du restaurant (preuve, qualité de service) | Numéro : **90 j** · ligne anonymisée : conservée |
| Transcription de conversation | Contenu intégral de ce que dit l'appelant | Intérêt légitime (diagnostic technique) | **30 j** |
| **Enregistrement audio** (#88) | **La voix** de l'appelant et celle de l'assistante, deux pistes séparées | Intérêt légitime : diagnostiquer et corriger la qualité de l'interaction | **30 j, et jamais plus que la transcription** |
| Comptes de l'admin | E-mail, empreinte du mot de passe | Exécution du contrat Helmane ↔ restaurant | Durée du contrat |

Durées réglables : `RETENTION_TRANSCRIPT_JOURS`, `RETENTION_NUMERO_JOURS`,
`RETENTION_RESERVATION_JOURS`, `RETENTION_ENREGISTREMENT_JOURS`.

⚠️ **La voix ne survit jamais à sa transcription.** `jours_enregistrement()` applique un
`min()` avec la durée du transcript : un réglage distrait ne peut pas garder l'audio plus
longtemps que le texte qu'il a produit. C'est un invariant vérifié par un garde-fou de
mutation, pas une intention.

## 3. La purge est appliquée, pas seulement écrite

Une durée de conservation qu'aucun code n'applique est **pire** que pas de durée : elle
donne une réponse fausse à qui la demande.

- `api/app/rgpd.py` purge automatiquement, **toutes les 24 h**, dès le démarrage ;
- les appels sont **anonymisés**, pas supprimés : le numéro et la transcription partent,
  la date, la durée et le coût restent. L'appel reste comptable — le compteur de forfait
  (#31) compte des lignes dans `calls` — mais l'appelant redevient inconnu ;
- les réservations sont supprimées, sur la **date** de la table et non sa création : une
  réservation prise six mois à l'avance ne disparaît pas avant d'avoir eu lieu ;
- **les enregistrements audio sont effacés du disque**, pas seulement oubliés en base. La
  purge est pilotée par la base — on demande à SQLite quels appels ont expiré, puis on
  reconstruit les chemins côté serveur. Balayer le dossier serait plus court et plus
  dangereux : on effacerait ce qui s'y trouve, y compris ce qu'on n'y a pas mis. Un
  balayage complémentaire ramasse les **orphelins**, ces fichiers dont plus aucune ligne
  ne parle — ce qui arrive après une restauration de sauvegarde ;
- **le journal de bord tombe avec le transcript** : il en cite des extraits, il ne doit
  pas lui survivre sous une autre forme ;
- **la supervision vérifie que la purge tourne** (`/supervision`, contrôle « Purge des
  données personnelles »). Deux cycles manqués = `panne`, donc alerte.

## 4. ⚠️ Des données sensibles transitent

**Une allergie alimentaire est une donnée de santé** (article 9). Ce n'est pas une
hypothèse :

- le champ « demandes particulières » d'une réservation est un texte libre ;
- le mot « allergie » est explicitement **renforcé** dans le vocabulaire Deepgram
  (`voice/bot.py`) — le produit est conçu pour l'entendre ;
- depuis #88, **l'enregistrement audio conserve en outre la voix elle-même**, qui est une
  donnée biométrique potentielle. C'est la raison de la durée courte et du plafonnement
  par la transcription ;
- la transcription conserve tout ce que l'appelant a dit.

Le prompt système interdit déjà à l'assistante de **garantir** quoi que ce soit sur les
allergènes, ce qui limite le risque sanitaire — mais pas la collecte. À arbitrer avec le
conseil juridique : consentement explicite, ou durée de conservation raccourcie pour ce
champ, ou refus de collecter.

## 5. ⚠️ Le traitement n'est pas européen, contrairement à ce qu'on croyait

Le ticket #22 indiquait « hébergement en Europe (déjà le cas) ». **C'est vrai du stockage,
faux du traitement.** Vérifié dans le code le 30/08/2026 :

| Fournisseur | Rôle | Où |
|---|---|---|
| Hostinger | VPS, base de données **et enregistrements audio** | 🇫🇷 Paris — vérifié |
| Modal | GPU, synthèse vocale | 🇪🇺 `MODAL_REGION=eu` — vérifié |
| **Deepgram** | **Transcription : reçoit l'audio de la conversation** | 🇺🇸 endpoint par défaut, **aucune région EU configurée** |
| **OpenRouter → Google** | **LLM : reçoit le texte de la conversation** | 🇺🇸 |
| **Twilio** | **Téléphonie : transporte l'appel** | 🇺🇸 |

Autrement dit : **le contenu des conversations quitte l'Union européenne.** Ce n'est pas
nécessairement interdit — cadres de transfert, clauses contractuelles types, Data Privacy
Framework — mais cela doit être **documenté dans le contrat de sous-traitance**, et le
restaurateur doit le savoir.

➡️ **À vérifier avant de signer** : Deepgram propose-t-il un point d'entrée européen sur
le plan payant ? Si oui, le configurer coûterait une variable d'environnement et
supprimerait le transfert le plus volumineux (tout l'audio).

## 6. Droits des personnes

| Droit | Ce qui existe aujourd'hui |
|---|---|
| Accès / portabilité | ⚠️ Manuel : requête SQL sur `calls` et `reservations` |
| **Effacement** (art. 17) | ✅ `rgpd.effacer_appelant("+336…")` — anonymise les appels, supprime les réservations |
| Rectification | ✅ Édition d'une réservation dans l'admin |
| Opposition | ⚠️ À traiter au cas par cas avec le restaurateur |

⚠️ **Limite à annoncer honnêtement lors d'une demande d'effacement** : la fonction ne
touche ni aux journaux du conteneur, ni aux **sauvegardes quotidiennes**, ni au journal
d'appels de Twilio. Les sauvegardes conservent la donnée jusqu'à leur propre expiration —
**14 jours**. Une réponse qui affirmerait l'effacement immédiat et total serait fausse.

✅ **Sauf pour l'audio.** Les enregistrements ne sont **pas** sauvegardés (choix
délibéré, cf. `scripts/backup-db.sh`) : sur eux, l'effacement est donc **complet et
immédiat**. C'est la donnée la plus sensible du produit, et c'est la seule sur laquelle
on peut répondre sans réserve.

## 7. Information de l'appelant

L'appelant entend, au décroché, après l'accueil du restaurant :

> *« Cet accueil est assuré par un assistant vocal ; votre appel est **enregistré et**
> traité pour votre réservation. »*

Le mot « enregistré » n'apparaît **que si l'enregistrement a réellement lieu** : le même
interrupteur pilote les deux (`enregistrement.actif()` exige `mention_active()`). On
n'enregistre donc jamais sans l'avoir dit — et on ne prétend jamais enregistrer sans le
faire.

Deux informations, les seules qui tiennent au téléphone : il parle à une machine, et ce
qu'il dit est traité. Le détail (durées, droits, contact) relève d'une information de
second niveau, à publier sur le site.

Aucun chemin de décroché ne peut sauter cette mention : elle est composée dans
`rgpd.accueil()`, utilisée par le TTS pré-rendu, le repli live et le mode `gather`, et
**un test échoue si un nouveau chemin prononce l'accueil sans elle**.

Désactivable par `RGPD_MENTION=0` — c'est une décision juridique, pas un réglage de
confort : couper la mention sans informer autrement expose le restaurateur.

## 8. Ce qui reste à faire

| # | Action | Qui |
|---|---|---|
| 1 | **Rédiger et faire signer le contrat de sous-traitance** (art. 28) | Helmi + conseil |
| 2 | Vérifier si Deepgram offre un point d'entrée européen | Helmi |
| 3 | Trancher le sort des données de santé (allergies) | Helmi + conseil |
| 4 | Publier l'information de second niveau sur le site | Helmi |
| 5 | Outiller le droit d'accès (aujourd'hui manuel) | code |
| 6 | ⚠️ **Faire valider la formulation de la mention** | Helmi + conseil |

**Réserve sur le point 6.** La mention annonce que l'appel est enregistré « pour votre
réservation ». Or la finalité réelle de l'audio est le **diagnostic de la qualité de
l'interaction**. Nommer une finalité incomplète est un défaut de transparence au sens de
l'article 13. Formulation retenue par Helmi le 31/08/2026 pour sa brièveté — une variante
plus exacte serait « enregistré pour améliorer la qualité du service et traiter votre
réservation », au prix d'environ une seconde de plus au décroché.
