# Passation — Helmane, assistant téléphonique IA (SaaS restaurants)

> Document de reprise pour une session Claude Code **connectée au WSL**, avec accès
> Docker, `gh`, `modal` et SSH vers le VPS. Objectif : autonomie — lancer et vérifier
> soi-même au lieu de faire copier-coller les sorties.
> Dépôt local : `~/moshi-rag-voice-assistant`. Branche de travail :
> `claude/moshi-rag-voice-assistant-0w8lsv`.
>
> Dernière mise à jour : 23/08/2026.

## 1. Le produit

SaaS quasi-zéro-budget : un assistant téléphonique qui répond 24/7 **en français** à la
place des restaurants débordés, renseigne et prend les réservations. Marque **Helmane**.
Voix retenue : **Moshi 1.6B de Kyutai** (la vraie voix d'unmute.sh), timbre « Développeuse ».
Médecins envisagés ensuite.

## 2. Architecture en production

```
Twilio ──webhook + media stream──► APP FastAPI + Pipecat
                                    VPS Hostinger, Paris, app.helmane.fr
                                    • STT Deepgram (ou Kyutai) + LLM OpenRouter
                                      (google/gemini-2.5-flash)
                                    • SQLite (réservations) + admin Jinja2/htmx
                                         │ websocket TTS
                                         ▼
                                    MODAL GPU serverless, région EU, scale-to-zero
                                    • moshi-server (Rust) : TTS Moshi 1.6B + ASR stt-1b-en_fr
```

Plus de ngrok, plus de PC allumé : l'app tourne 24/7 sur le VPS derrière Caddy (HTTPS auto).

## 3. Accès

| Quoi | Où |
|---|---|
| VPS | `ssh root@187.77.172.87` (alias conseillé : `ssh helmane`) |
| Projet sur le VPS | `/opt/moshi-rag-voice-assistant` (suit la branche `main`) |
| Base de production | `/var/lib/docker/volumes/moshi-rag-voice-assistant_api_data/_data/app.db` |
| Sauvegardes | `/opt/backups/db/app-*.db.gz`, cron `0 4 * * *` |
| Admin | https://app.helmane.fr/admin |
| TTS Modal | `wss://helmi75--moshi-server-tts-server.modal.run` |
| Dépôt | https://github.com/helmi75/moshi-rag-voice-assistant |

## 4. ✅ Régression de sécurité — refermée durablement le 23/08

**Ce qui était ouvert.** `http://187.77.172.87:8000/admin` répondait `HTTP 307`
(`server: uvicorn`) sur IPv4 **et** IPv6 : l'API était joignable en clair, hors Caddy et
hors TLS. `ufw` était inactif. Le conteneur tournait ainsi depuis au moins 4 jours.

**Ce qui a été fait.**

1. Port ramené sur la boucle locale — vérifié depuis l'extérieur : `curl` sur 8000
   n'aboutit plus, ni en IPv4 ni en IPv6, tandis que `https://app.helmane.fr/health`
   répond 200 et que le webhook Twilio reste joignable.
2. `ufw` activé (22/80/443) et persistant au reboot. **Il ne protège rien de plus
   aujourd'hui** : hors Docker, seul `sshd` écoute. Sa valeur est future — empêcher
   qu'un service lancé plus tard sur l'hôte soit exposé par distraction.
3. **Le correctif est dans `main`** (PR #80) : `docker-compose.yml` publie
   `127.0.0.1:8000:8000`. Un `docker-compose.override.yml` avait d'abord été posé sur le
   VPS comme rustine ; il a été **supprimé** une fois la branche fusionnée, sinon il
   aurait masqué en silence toute évolution légitime des ports.

   Vérifié le 23/08 sur la machine : plus d'override, prod sur `ea6cca2`, et
   `docker inspect` du conteneur `api` montre `8000/tcp -> 127.0.0.1:8000`.

4. **Le déploiement le revérifie tout seul.** `scripts/deploy.sh` teste le port 8000
   depuis le poste, pas depuis le serveur : ce qui compte est ce qu'un tiers voit. Ce
   port a été rouvert deux fois par un `git pull`, découvert par hasard les deux fois.

**Recherche d'intrusion (23/08).** Aucun signe : 2 comptes, tous deux créés le 30/07
(`superadmin` + le restaurateur de démo), aucun ajouté ; 1 établissement, 17 réservations,
24 appels — l'état attendu ; aucune écriture depuis le 31/07. Sauvegardes quotidiennes
présentes pour comparaison.

⚠️ **Limite de cette recherche** : les journaux du conteneur couvrant la période
d'exposition ont été perdus en le recréant pour appliquer le correctif. L'application ne
tient par ailleurs aucun journal des tentatives de connexion. L'absence de preuve n'est
donc pas une preuve d'absence.

⚠️ **`ufw` ne protège PAS les ports publiés par Docker** (chaîne `DOCKER-USER`, évaluée
avant ufw — elle est vide sur ce serveur). Ce qui ferme le port 8000, c'est l'écoute sur
`127.0.0.1`, jamais le pare-feu.

## 4 bis. ⚠️ Jeton Twilio refusé en production (trouvé par la supervision)

`TWILIO_AUTH_TOKEN` du `.env` de production est **rejeté par Twilio (HTTP 401)**, sur
l'API standard comme sur Monitor. Vraisemblablement révoqué après l'incident #20 et jamais
reporté. Vérifié le 24/08 : le conteneur reçoit bien un SID de 34 caractères et un jeton
de 32 — ce n'est pas un problème de câblage, c'est le jeton lui-même.

**Ce que ça ne casse pas** : les appels entrants. C'est Twilio qui nous appelle ; il n'a
besoin d'aucun jeton pour ça. D'où le silence complet jusqu'ici.

**Ce que ça casse** : toute action sortante vers l'API Twilio — dont la relève des alertes
de webhook, seul contrôle capable de voir les appels qui meurent AVANT d'atteindre
l'application. Ce contrôle reste donc en « attention » tant que le jeton n'est pas renouvelé.

➡️ **À faire (nécessite la console Twilio)** : copier le *Auth Token* courant, le poser
dans `/opt/moshi-rag-voice-assistant/.env`, puis `docker compose up -d api`. La sonde
repassera au vert toute seule à la relève suivante (15 min).

## 5. Écart entre la branche et la production

**La prod tourne sur `main` (`ea6cca2`)**, déployée par `scripts/deploy.sh`, qui refuse de
partir si la branche n'est pas `main`, si l'arbre est sale, si `HEAD ≠ origin/main` ou si
la CI n'est pas verte **pour ce commit exact**.

Tout est fusionné et déployé au 24/08 (PR #82, #83, #84). La branche de travail ne
contient rien de non livré.

⚠️ Ne pas ouvrir de pull request sans demande explicite de l'utilisateur.

## 6. Ce qui marche (vérifié)

- ✅ Appel Twilio réel de bout en bout : voix Développeuse, conversation, réservation en base.
- ✅ Blanc médian mesuré **1,16 s** (p90 1,58 s). Coût mesuré **11,4 ¢/appel**.
- ✅ 284 tests, aucun appel réseau (vérifié : la relève Twilio est coupée par conftest).
  CI GitHub Actions verte, contrôle de mutation 12/12.
- ✅ Conteneurs remontés seuls après reboot (critère de résilience de `DEPLOY.md`).
- ✅ Sauvegarde quotidienne **avec test de restauration réussi** (`ok / 17 réservations / 1 établissement`).
- ✅ Admin v3, voix par établissement (catalogue fermé de 7 voix), latence instrumentée en base.
- ✅ Supervision (#24) : sonde `/supervision` (9 contrôles, aucun appel réseau), surveillée
  **depuis GitHub Actions** — donc de l'extérieur du serveur. Voir `docs/SUPERVISION.md`.
- ✅ **Chaîne d'alerte éprouvée dans les deux sens** le 24/08, pas supposée : jeton correct
  → run réussi avec simple avertissement ; jeton volontairement faux → run **en échec**,
  donc e-mail ; jeton rétabli → run réussi. Trois passages réels (`32772320693`,
  `32772387371`, `32772457217`).

## 7. Suivi du travail — GitHub Issues

69 tickets (#10 à #78), 10 epics. Les 20 tickets ouverts portent un **label de phase** qui
encode la séquence logique :

| Phase | Label | Tickets |
|---|---|---|
| ① Fiabiliser | `phase-1-fiabiliser` | ~~#21~~ #23 ~~#24~~ ~~#25~~ ~~#39~~ |
| ② Tenir la charge | `phase-2-charge` | #26 #27 #28 #40 |
| ③ Vendre | `phase-3-vendre` | #22 #29 #30 #31 #32 |
| ④ Industrialiser | `phase-4-industrialiser` | #33 → #38 |

Priorités : `P0` … `P3`. Un Project board existe côté GitHub (créé par l'utilisateur).

`scripts/link_subissues.sh` rattache les 59 tickets à leurs epics en vraies sous-issues
(barres de progression). **Pas encore exécuté** — à lancer avec `gh` authentifié.

## 8. Prochaines étapes

1. **Renouveler le jeton Twilio** (§4 bis) — 2 minutes dans la console, et le dernier
   contrôle en « attention » repasse au vert.
2. **#23 reste ouvert** : les sauvegardes sont sur la même machine que la base. Un incident
   disque emporte tout. Les sortir du serveur reste à faire. La supervision surveille
   désormais leur *fraîcheur*, pas leur *survie* à une panne disque.
3. **#29 grille tarifaire** — proposition : Essentiel 89 € (150 appels), Service 149 €
   (400), Maison 249 € (5 établissements). Marché FR : Yumcall 99 €, Nerolia 149 €,
   Accueil IA 39-149 €. Coût de revient 11,4 ¢/appel → marge > 80 % à 89 €. Débloque la
   page de vente, les plafonds et Stripe.
4. **#40 volontairement ouvert** : `max_containers=4` est déployé mais le plafond
   lui-même n'est pas prouvé (il faudrait plus de 32 sessions simultanées).
5. **#30 numéro de démo** — bloqué côté Twilio, voir §9.

## 9. Twilio — dossier réglementaire en attente

Numéro visé : **+33 1 59 48 01 08** (géographique Paris). La France impose un dossier ARCEP.

- Bundle **`Helmane - FR Local Individual`**, SID `BUfa38e4d74f230af745adc8d5e9cf96c8`
- Type : Direct Customer · Local · **Individual**
- Statut : **awaiting review** (soumis le 23/08). Réponse par e-mail sous quelques jours ouvrés.
- Une fois approuvé : sur le numéro → *Assign approved Bundle* → chercher « Helmane ».

Le numéro Twilio actuel continue de fonctionner : rien n'est bloqué en attendant.

**Conséquence produit** : chaque numéro français exige un dossier conforme, ce qui percute
la promesse d'onboarding « moins de 15 minutes » (#37). À trancher : stock de numéros portés
par Helmane, ou justificatifs fournis par chaque restaurant.

## 10. Commandes utiles

```bash
# Sur le VPS
docker compose -f /opt/moshi-rag-voice-assistant/docker-compose.yml ps
docker compose -f /opt/moshi-rag-voice-assistant/docker-compose.yml logs -f --tail=50 api
curl -s localhost:8000/health
/opt/backups/backup-db.sh                      # sauvegarde à la demande

# En local
cd api && python -m pytest tests/ -q            # 284 tests, aucun réseau
python3 scripts/mutation_check.py               # 12 garde-fous : chacun doit MORDRE
modal deploy deploy/modal_moshi_server.py
python scripts/test_moshi_server.py --url https://helmi75--moshi-server-tts-server.modal.run

# Garde-fou après chaque déploiement — à lancer depuis le WSL, pas depuis le VPS :
# ce qui compte est ce qu'un tiers voit, pas ce que la machine croit exposer.
curl -sI --max-time 8 http://187.77.172.87:8000/ >/dev/null 2>&1 \
  && echo "ALERTE : port 8000 joignable publiquement" || echo "OK : 8000 fermé"

# État réel de la pile (le jeton vit dans le .env du VPS, jamais ici) :
curl -s -H "X-Supervision-Token: $SUPERVISION_TOKEN" \
  https://app.helmane.fr/supervision | jq '.niveau, .controles[] | .titre + " : " + .niveau'
```

## 11. Contraintes et règles

- **Secrets** : uniquement dans `.env` (non versionné), **jamais** dans `env.example`.
  Ne jamais afficher les **valeurs** des clés, seulement leur présence. Les clés
  historiquement exposées ont été révoquées (#20 fermé).
- Développer et pousser **uniquement** sur `claude/moshi-rag-voice-assistant-0w8lsv`.
- **Ne pas créer de pull request** sans demande explicite.
- Ne jamais inscrire d'identifiant de modèle dans un commit, une PR ou un commentaire de code.
- Les SID Twilio (`AC…`, `BU…`) sont des identifiants publics ; le **Auth Token** est le secret.
