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

## 4. ✅ Régression de sécurité — refermée le 23/08, mais pas encore durablement

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
3. **`docker-compose.override.yml` posé sur le VPS** (non versionné, donc insensible à
   `git pull`) :

   ```yaml
   services:
     api:
       ports: !override
         - "127.0.0.1:8000:8000"
   ```

   `!override` **remplace** la liste au lieu de s'y ajouter : Compose fusionne les
   séquences par concaténation, donc sans ce marqueur `0.0.0.0:8000` resterait publié.
   Vérifié en conditions réelles : l'arbre git du VPS est **propre**, son
   `docker-compose.yml` suivi publie toujours `8000:8000`, et le port reste malgré tout
   fermé de l'extérieur.

**Pourquoi ce n'est pas encore réglé.** La protection vit dans un fichier propre à cette
machine. Le correctif de fond est le commit `9a6801e` de la branche de travail : **tant
qu'il n'est pas dans `main`, le dépôt continue de décrire une configuration non sûre**, et
tout nouveau serveur déployé depuis `main` naîtrait avec le port ouvert.

➡️ **À faire : fusionner la branche dans `main`, puis supprimer
`docker-compose.override.yml` du VPS** — sinon il masquera silencieusement toute évolution
légitime des ports.

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

## 5. Écart entre la branche et la production

**La prod tourne sur `main` (v1.0.0, `1fcd6be`).** La branche de travail contient 5 commits
qui ne sont PAS déployés :

| Commit | Contenu |
|---|---|
| `00d0aed` | Prompt système v2 (prononciation, dates relatives, garde-fous) + `@modal.concurrent` + `max_containers` |
| `576217b` | Backlog complet (`docs/backlog-jira.csv`) |
| `1e646d0` | `scripts/link_subissues.sh` |
| `5ca094e` | `scripts/backup-db.sh` + procédure de restauration dans `DEPLOY.md` |
| `9a6801e` | API sur `127.0.0.1` uniquement |

**Tant que ce n'est pas fusionné dans `main`, chaque `git pull` sur le VPS rouvrira le
port 8000.** Fusionner est donc la vraie correction. ⚠️ Ne pas ouvrir de pull request
sans demande explicite de l'utilisateur.

## 6. Ce qui marche (vérifié)

- ✅ Appel Twilio réel de bout en bout : voix Développeuse, conversation, réservation en base.
- ✅ Blanc médian mesuré **1,16 s** (p90 1,58 s). Coût mesuré **11,4 ¢/appel**.
- ✅ 220 tests, aucun appel réseau. CI GitHub Actions verte.
- ✅ Conteneurs remontés seuls après reboot (critère de résilience de `DEPLOY.md`).
- ✅ Sauvegarde quotidienne **avec test de restauration réussi** (`ok / 17 réservations / 1 établissement`).
- ✅ Admin v3, voix par établissement (catalogue fermé de 7 voix), latence instrumentée en base.

## 7. Suivi du travail — GitHub Issues

69 tickets (#10 à #78), 10 epics. Les 20 tickets ouverts portent un **label de phase** qui
encode la séquence logique :

| Phase | Label | Tickets |
|---|---|---|
| ① Fiabiliser | `phase-1-fiabiliser` | #21 #23 #24 #25 #39 |
| ② Tenir la charge | `phase-2-charge` | #26 #27 #28 #40 |
| ③ Vendre | `phase-3-vendre` | #22 #29 #30 #31 #32 |
| ④ Industrialiser | `phase-4-industrialiser` | #33 → #38 |

Priorités : `P0` … `P3`. Un Project board existe côté GitHub (créé par l'utilisateur).

`scripts/link_subissues.sh` rattache les 59 tickets à leurs epics en vraies sous-issues
(barres de progression). **Pas encore exécuté** — à lancer avec `gh` authentifié.

## 8. Prochaines étapes

1. **Refermer la régression du §4**, puis fusionner la branche pour qu'elle ne revienne pas.
2. **#25 + #39** — `modal deploy deploy/modal_moshi_server.py`, puis **deux appels
   simultanés depuis deux téléphones**. Critère : **un seul conteneur** dans le tableau de
   bord Modal (avant le correctif, il y en avait deux). Le code est écrit, la preuve manque.
3. **#29 grille tarifaire** — proposition : Essentiel 89 € (150 appels), Service 149 €
   (400), Maison 249 € (5 établissements). Marché FR : Yumcall 99 €, Nerolia 149 €,
   Accueil IA 39-149 €. Coût de revient 11,4 ¢/appel → marge > 80 % à 89 €. Débloque la
   page de vente, les plafonds et Stripe.
4. **#23 reste ouvert** : les sauvegardes sont sur la même machine que la base. Un incident
   disque emporte tout. Les sortir du serveur reste à faire.
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
cd api && python -m pytest tests/ -q            # 220 tests, aucun réseau
modal deploy deploy/modal_moshi_server.py
python scripts/test_moshi_server.py --url https://helmi75--moshi-server-tts-server.modal.run

# Garde-fou après chaque déploiement — à lancer depuis le WSL, pas depuis le VPS :
# ce qui compte est ce qu'un tiers voit, pas ce que la machine croit exposer.
curl -sI --max-time 8 http://187.77.172.87:8000/ >/dev/null 2>&1 \
  && echo "ALERTE : port 8000 joignable publiquement" || echo "OK : 8000 fermé"
```

## 11. Contraintes et règles

- **Secrets** : uniquement dans `.env` (non versionné), **jamais** dans `env.example`.
  Ne jamais afficher les **valeurs** des clés, seulement leur présence. Les clés
  historiquement exposées ont été révoquées (#20 fermé).
- Développer et pousser **uniquement** sur `claude/moshi-rag-voice-assistant-0w8lsv`.
- **Ne pas créer de pull request** sans demande explicite.
- Ne jamais inscrire d'identifiant de modèle dans un commit, une PR ou un commentaire de code.
- Les SID Twilio (`AC…`, `BU…`) sont des identifiants publics ; le **Auth Token** est le secret.
