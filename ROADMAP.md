# Roadmap — SaaS d'accueil téléphonique IA

**Objectif** : un SaaS qui répond au téléphone à la place des commerces débordés d'appels
(restaurants d'abord, cabinets médicaux ensuite) : renseigner les clients, prendre des
réservations et des rendez-vous, 24h/24, sans faire attendre personne.

**Positionnement** : chaque client (« tenant ») a son numéro de téléphone, sa base de
connaissances (horaires, menu, adresse, FAQ) et ses actions métier (réserver une table,
prendre un RDV). L'onboarding doit prendre moins de 15 minutes.

---

## Phase 1 — Cerveau conversationnel multi-tenant ✅ (fait)

Le socle produit, testable immédiatement par téléphone :

- [x] Routage multi-tenant par numéro appelé (champ Twilio `To` → tenant en base SQLite)
- [x] LLM Claude avec function calling (`check_availability`, `create_reservation`)
- [x] Base de connaissances par tenant injectée dans le prompt système
- [x] Réservations persistées en SQLite, rattachées au tenant
- [x] Mémoire de conversation par appel (`CallSid`)
- [x] Boucle vocale Twilio Gather/Say (STT/TTS de Twilio) + webhook SMS
- [x] Abandon de Moshi et du GPU : APIs cloud uniquement, un simple VPS suffit
- [x] Tests unitaires (LLM mocké) + script e2e

**Limites assumées de cette phase** : latence de 2 à 4 s par tour (Gather/Say n'est pas
du streaming), voix TTS Twilio standard, pas d'interruption possible (barge-in limité).
C'est suffisant pour valider le produit avec un premier restaurant pilote.

**Jalon de sortie** : 1 restaurant pilote qui reçoit de vrais appels pendant 2 semaines.

## Phase 2 — Voix temps réel (streaming)

Remplacer la boucle Gather/Say par un pipeline audio streaming, latence cible ~1-1,3 s.
Stack arrêtée après étude de l'état de l'art open source — détail, comparatifs et
sources dans **[docs/VOICE_STACK.md](docs/VOICE_STACK.md)** :

- [x] Twilio **Media Streams** (WebSocket audio bidirectionnel) — `VOICE_MODE=stream`
- [x] Orchestration **Pipecat** (open source, Python, étages STT/LLM/TTS interchangeables)
      — `api/app/voice/bot.py`
- [x] STT streaming français : **Deepgram** (API, phase A) — bascule **Kyutai STT**
      auto-hébergé prévue en phase B (interface Pipecat identique)
- [x] TTS streaming français : **Cartesia** (API, phase A) — bascule **Kyutai TTS 1.6B**
      / **Pocket TTS** (CPU) prévue en phase B
- [x] Barge-in et détection de fin de tour (VAD Silero + smart-turn v3, embarqués)
- [x] LLM : API Claude conservée (function calling via Pipecat, mêmes outils/prompts)
- Le module `llm.py` (tenant + outils) est réutilisé tel quel : seul le transport audio change.

**Coût estimé par minute d'appel** (phase A, tout API) : STT ~0,005 $ + LLM ~0,01-0,03 $
+ TTS ~0,02-0,05 $ + Twilio ~0,01 $ ≈ **0,05 à 0,10 $/min**. À 500 min/mois par client,
marge confortable sur un abonnement à 99-199 €/mois. Référence latence : Unmute (Kyutai)
prouve < 1 s avec ces mêmes briques.

## Phase 3 — Couche SaaS

Ce qui transforme le pipeline en produit vendable en self-service :

- [ ] Dashboard web (Next.js) : onboarding d'un business, édition de la KB, achat du numéro
      Twilio en un clic (API Twilio), transcripts et enregistrements des appels
- [ ] Auth (Clerk/Auth0) + organisations
- [ ] Passage de SQLite à PostgreSQL, conversations en Redis
- [ ] Facturation Stripe : abonnement + dépassement à la minute
- [ ] Notifications : SMS de confirmation de réservation au client final, email/SMS
      récapitulatif au commerçant
- [ ] Transfert d'appel vers un humain (mots-clés « urgence », demande explicite)
- [ ] Observabilité : logs structurés, alerting, tableau de bord qualité (taux de
      résolution sans humain, durée moyenne, sujets d'appel)

**Jalon de sortie** : 10 clients payants onboardés sans intervention manuelle.

## Phase 4 — Verticales et intégrations

- [ ] **Verticale médecins** : prise de RDV, rappels, garde/urgences → exige RGPD strict,
      hébergement HDS (OVHcloud/Scaleway certifiés), DPA, minimisation des données de
      santé. À lancer seulement une fois le produit prouvé sur les restaurants.
- [ ] Intégrations réservation : Google Calendar, TheFork/Zenchef (restaurants),
      Doctolib n'ayant pas d'API publique → agenda propre + export iCal pour les médecins
- [ ] Multi-langue par tenant (fr/en/ar…)
- [ ] Base de connaissances enrichie : ingestion de documents (PDF menus, site web) avec
      embeddings + vector store quand les KB dépassent la taille d'un prompt
- [ ] Numéros et téléphonie locale (portabilité, SIP trunking pour réduire les coûts)
- [ ] **Option 100 % local / souverain** : Kyutai STT + Kyutai TTS + Qwen3 8B quantisé
      (AWQ, vLLM) tiennent ensemble sur **une RTX 4090 louée (~150-250 €/mois)** et
      servent des dizaines d'appels simultanés. À déclencher quand : volume > ~2 000
      min/mois, ou client santé (argument RGPD « aucune donnée ne sort du serveur »),
      ou besoin de latence < 1 s. Grille de coûts et seuils dans docs/VOICE_STACK.md.

---

## Principes techniques

1. **Pas de GPU, pas de modèle auto-hébergé** tant que le volume ne le justifie pas :
   tout en API (paiement à l'usage, coût nul sans trafic).
2. **Le différenciateur est le cerveau métier multi-tenant**, pas le pipeline audio :
   la logique tenant/outils/KB (`api/app/llm.py`, `tenants.py`) doit rester indépendante
   du transport (webhook aujourd'hui, WebSocket demain).
3. **Vendre avant de sur-construire** : chaque phase a un jalon commercial, pas seulement
   technique.
