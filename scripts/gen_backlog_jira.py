#!/usr/bin/env python3
"""Génère le backlog Jira (CSV d'import) à partir de l'historique réel du projet.

Sources : CHANGELOG.md, ROADMAP.md, docs/PASSATION.md, 78 commits sur main, PR #9.
Le CSV est écrit avec le module csv pour que l'échappement (virgules, guillemets,
retours à la ligne dans les descriptions) soit correct à coup sûr.
"""
import csv
import pathlib

# (type, résumé, description, priorité, statut, labels, epic)
E = "Epic"
S = "Story"
T = "Task"
B = "Bug"

EPICS = [
    ("SOCLE", "Socle conversationnel multi-tenant",
     "Le cerveau du produit : router l'appel vers le bon établissement, comprendre la "
     "demande, appeler les outils métier et persister la réservation."),
    ("VOIX", "Voix temps réel Moshi (Kyutai)",
     "Remplacer la boucle Gather/Say par un pipeline audio en streaming avec la voix "
     "Moshi 1.6B servie par moshi-server (Rust) sur GPU serverless."),
    ("LATENCE", "Latence et naturalité de la conversation",
     "Tout ce qui sépare une démo d'un vrai standard : le blanc avant la réponse, la "
     "coupure de parole, la fin de tour, le raccroché."),
    ("ADMIN", "Plateforme d'administration",
     "L'interface où le restaurateur gère son établissement et où le super-admin "
     "surveille le parc, la santé et les coûts."),
    ("PROD", "Mise en production et hébergement",
     "Sortir du poste local + ngrok : domaine, HTTPS, VPS qui survit au reboot, "
     "sauvegardes et supervision."),
    ("QUALITE", "Qualité, tests et intégration continue",
     "Filet de sécurité : tests sans réseau, CI sur chaque push, non-régression."),
    ("SCALE", "Montée en charge et coûts GPU",
     "Servir plusieurs appels simultanés sans multiplier les GPU, et plafonner la "
     "facture. Le GPU est le seul vrai poste de coût variable."),
    ("SECU", "Sécurité et conformité",
     "Rotation des secrets, protection de l'admin, obligations RGPD sur les données "
     "d'appelants."),
    ("GTM", "Offre commerciale et mise sur le marché",
     "Passer d'un produit qui marche à un produit qu'on vend : prix, page de vente, "
     "démo, premier client pilote."),
    ("SAAS", "Couche SaaS en libre-service",
     "Ce qui permet d'onboarder un client sans intervention manuelle : facturation, "
     "numéro en un clic, notifications."),
]

ROWS = [
    # ---------------------------------------------------------------- SOCLE
    (S, "Router l'appel vers le bon établissement par le numéro appelé",
     "Le champ Twilio `To` identifie l'établissement en base (tenants.get_by_phone). "
     "Le tenant de démo est réaligné automatiquement sur TWILIO_NUMBER au démarrage, "
     "ce qui évite le « ce numéro n'est pas encore configuré » quand un mauvais numéro "
     "s'était figé dans le volume Docker.",
     "High", "Done", ["backend", "multi-tenant"], "SOCLE"),
    (S, "Outils métier check_availability et create_reservation",
     "Function calling : le modèle vérifie la disponibilité avant de confirmer, puis "
     "enregistre la réservation. Seul l'appel à create_reservation crée la ligne — le "
     "dire à l'oral ne réserve rien.",
     "High", "Done", ["backend", "llm"], "SOCLE"),
    (S, "Base de connaissances par établissement injectée dans le prompt",
     "Menu, horaires, adresse, FAQ. À cette échelle un RAG vectoriel n'apporte rien : "
     "la fiche tient dans le prompt système (cf. ARCHITECTURE.md).",
     "Medium", "Done", ["llm"], "SOCLE"),
    (S, "Persistance des réservations en SQLite, rattachées au tenant",
     "Tables tenants et reservations. Migrations successives : numéro de l'appelant "
     "(v3), latence mesurée (v4), voix par établissement (v5).",
     "High", "Done", ["backend", "bdd"], "SOCLE"),
    (S, "Webhooks Twilio voix et SMS",
     "POST /twilio/voice, /twilio/sms et un routeur générique /twilio/webhook qui "
     "dispatche selon CallSid ou Body.",
     "High", "Done", ["twilio"], "SOCLE"),
    (T, "Mode gather de repli avec voix neuronale française",
     "Polly.Lea-Neural via <Say>. Zéro latence, zéro clé, inclus dans Twilio. Sert de "
     "repli si le pipeline streaming est indisponible.",
     "Low", "Done", ["twilio", "tts"], "SOCLE"),
    (B, "Accueil personnalisé écrasé au redémarrage par le seed de démo",
     "Le seed du tenant de démo réécrasait le message d'accueil personnalisé à chaque "
     "démarrage du conteneur.",
     "Medium", "Done", ["bug", "backend"], "SOCLE"),

    # ---------------------------------------------------------------- VOIX
    (S, "Pipeline streaming Twilio Media Streams + Pipecat",
     "WebSocket audio bidirectionnel, orchestration Pipecat avec étages STT/LLM/TTS "
     "interchangeables (VOICE_MODE=stream). Le module llm.py est réutilisé tel quel : "
     "seul le transport audio change.",
     "Highest", "Done", ["voix", "pipecat"], "VOIX"),
    (T, "Évaluer Pocket TTS sur CPU puis sur GPU, et l'abandonner",
     "Sur CPU, french_24l met 5 à 10 s à produire son premier morceau : voix saccadée. "
     "Le GPU rend la génération temps réel mais impose une machine dédiée. Piste "
     "abandonnée au profit du serveur Rust moshi-server.",
     "Low", "Done", ["voix", "abandonné"], "VOIX"),
    (S, "Déployer moshi-server (Rust) sur Modal en GPU serverless",
     "moshi-server 0.6.4, image CUDA 12.8, GPU L4, CUDA graphs et batching, "
     "scale-to-zero. Sert la vraie voix d'unmute.sh, au moins en temps réel à chaud.",
     "Highest", "Done", ["voix", "modal", "gpu"], "VOIX"),
    (B, "Build CUDA sans GPU : figer CUDA_COMPUTE_CAP au moment du build",
     "Les kernels se compilent à la construction de l'image, où aucun GPU n'est "
     "disponible : la capacité de calcul doit être fournie explicitement.",
     "High", "Done", ["bug", "modal"], "VOIX"),
    (B, "Édition de liens libpython pour le module tts_py (pyo3)",
     "tts_py se lie à la libpython. Il faut LIBRARY_PATH au build et LD_LIBRARY_PATH "
     "sur le LIBDIR Python au démarrage.",
     "High", "Done", ["bug", "modal"], "VOIX"),
    (B, "Erreur 429 de Hugging Face au téléchargement des voix",
     "Le glob par défaut télécharge tout le dépôt de voix. Restreint au dossier "
     "unmute-prod-website, puis catalogue curaté embarqué dans l'image.",
     "Medium", "Done", ["bug", "modal"], "VOIX"),
    (S, "Client TTS Pipecat pour moshi-server",
     "api/app/voice/moshi_server_tts.py : websocket msgpack, un message Text par mot "
     "puis Eos, réception de PCM float32 à 24 kHz. open_timeout élevé pour tolérer le "
     "cold start au handshake.",
     "High", "Done", ["voix", "pipecat"], "VOIX"),
    (T, "Script de fumée du serveur TTS, sans Twilio",
     "scripts/test_moshi_server.py : mesure audio_sec / wall, écrit un WAV 24 kHz. "
     "Sert aussi à réveiller le serveur avant un appel.",
     "Medium", "Done", ["outillage", "voix"], "VOIX"),
    (S, "Kyutai STT en option, sur le même serveur moshi-server",
     "Module ASR stt-1b-en_fr servi par le même process (/api/asr-streaming). Français "
     "natif, un fournisseur externe de moins, environ 1,5 ¢ de moins par appel. "
     "Bascule par une seule variable : STT_PROVIDER=deepgram|kyutai.",
     "Medium", "Done", ["stt", "voix"], "VOIX"),
    (T, "Confirmer Kyutai STT à l'oreille sur un vrai appel avant d'en faire le défaut",
     "Validé hors ligne : transcription française parfaite, flush de fin de tour 300 à "
     "430 ms, session qui survit à 90 s de silence. Reste à confirmer sur du µ-law "
     "8 kHz réel. Repli immédiat : STT_PROVIDER=deepgram.",
     "Medium", "To Do", ["stt", "voix"], "VOIX"),
    (S, "Catalogue fermé de 7 voix françaises, choisies par établissement",
     "La liste est fermée volontairement : moshi-server ne refuse PAS une voix "
     "inconnue, il la remplace en silence par sa voix de repli (256 Hz mesurés contre "
     "131 à 242 Hz pour le catalogue). Sans garde-fou, tous les appels sonneraient faux "
     "sans un seul log.",
     "Medium", "Done", ["voix", "admin"], "VOIX"),

    # ---------------------------------------------------------------- LATENCE
    (S, "Audit de latence chiffré, avant et après",
     "Mesures instrumentées : -45 % sur le TTS, -79 % sur le premier tour.",
     "High", "Done", ["latence"], "LATENCE"),
    (S, "Accueil pré-enregistré et préchauffage du serveur TTS",
     "Au décroché, un WAV déjà rendu en voix Développeuse est joué immédiatement "
     "pendant qu'un ping réveille moshi-server : l'appelant n'entend jamais le cold "
     "start. Musique d'attente en secours si la première réponse tarde.",
     "High", "Done", ["latence", "ux"], "LATENCE"),
    (S, "Préchauffage du LLM pendant l'accueil",
     "Premier tour mesuré à 4,5 s contre 0,5 s ensuite : connexion HTTPS froide et "
     "prompt non préchauffé côté fournisseur. Un jeton envoyé pendant l'accueil ramène "
     "le premier vrai tour au régime nominal, pour environ 0,02 centime.",
     "Medium", "Done", ["latence", "llm"], "LATENCE"),
    (B, "PANNE DE PRODUCTION : tous les appels muets (reasoning en argument nu)",
     "Le paramètre reasoning passé en mot-clé nu levait un TypeError dans le SDK "
     "OpenAI et rendait TOUS les appels muets. La forme correcte est extra_body. "
     "Régression introduite par le correctif de latence précédent.",
     "Highest", "Done", ["bug", "production", "llm"], "LATENCE"),
    (B, "Blanc de 6 s avant chaque appel d'outil (raisonnement Gemini)",
     "Gemini 2.5 « réfléchit » avant de répondre : au téléphone c'est du silence pur. "
     "Mesuré 6,16 s contre 0,59 s une fois coupé (LLM_REASONING=off), sans aucune "
     "perte sur l'extraction date/heure/nom. L'appelant, lui, disait « allô ».",
     "Highest", "Done", ["bug", "latence", "llm"], "LATENCE"),
    (B, "Coupures de parole déclenchées par le bruit ambiant",
     "Pipecat ouvrait un tour sur VAD OU transcription : un souffle tranchait la phrase "
     "et rien ne repartait. Corrigé par INTERRUPTION=mots.",
     "High", "Done", ["bug", "latence", "vad"], "LATENCE"),
    (B, "Relance incongrue après un au revoir",
     "Après une prise de congé, le silence de l'appelant est normal : il a terminé. La "
     "relance d'inactivité repartait au pire moment. L'assistante raccroche désormais.",
     "Medium", "Done", ["bug", "ux"], "LATENCE"),
    (S, "Mesurer le blanc ressenti par l'appelant, tour par tour, en base",
     "Les journaux de conteneur repartent de zéro à chaque déploiement : la mesure est "
     "stockée en base (migration v4). Résultat figé : blanc médian 1,16 s, p90 1,58 s.",
     "High", "Done", ["latence", "observabilite"], "LATENCE"),
    (T, "Figer les réglages de naturalité validés à l'oreille",
     "DEEPGRAM_MODEL=nova-3 (noms propres), VAD_STOP_SECS=0.5, INTERRUPTION=mots, "
     "LLM_REASONING=off. Tous surchargeables par variable d'environnement, sans "
     "redéploiement.",
     "Medium", "Done", ["latence", "config"], "LATENCE"),
    (S, "Épingler le GPU Modal en région EU",
     "Chaque mot d'un énoncé fait un aller-retour websocket : un GPU aux États-Unis "
     "depuis une app en France ajoutait environ 150 ms par échange. MODAL_REGION=eu par "
     "défaut.",
     "High", "Done", ["latence", "modal"], "LATENCE"),

    # ---------------------------------------------------------------- ADMIN
    (S, "Plateforme admin v1 : tableau de bord à deux rôles",
     "FastAPI + Jinja2 + htmx. Restaurateur et super-admin.",
     "High", "Done", ["admin"], "ADMIN"),
    (T, "Refonte visuelle de l'admin, thèmes clair et sombre",
     "Jeu de tokens CSS, contrastes mesurés sur les deux surfaces.",
     "Medium", "Done", ["admin", "design"], "ADMIN"),
    (S, "Admin v3 : coquille latérale, vue du parc, santé et coûts",
     "Salle de contrôle par établissement. Tout ce qui est affiché est mesuré — aucune "
     "métrique inventée.",
     "High", "Done", ["admin"], "ADMIN"),
    (B, "Fuite de données entre établissements dans les lignes de réservation",
     "Le nom des autres enseignes apparaissait chez un restaurateur après une édition. "
     "Scoping tenant manquant sur la requête.",
     "Highest", "Done", ["bug", "securite", "admin"], "ADMIN"),

    # ---------------------------------------------------------------- PROD
    (S, "Hébergement 24/7 : Caddy HTTPS automatique et image de production allégée",
     "Politique de redémarrage, image sans torch ni moshi pour un VPS CPU qui n'utilise "
     "que moshi_server et Deepgram. Documenté dans docs/DEPLOY.md.",
     "High", "Done", ["infra", "prod"], "PROD"),
    (S, "Domaine helmane.fr avec Caddy multi-noms",
     "Accepter plusieurs noms d'hôte permet de basculer de domaine sans fenêtre de "
     "coupure sur le webhook Twilio.",
     "High", "Done", ["infra", "prod"], "PROD"),
    (S, "Migrer l'application sur Hostinger, hors de Modal et de ngrok",
     "L'app tourne sur un VPS CPU en Europe, sur app.helmane.fr. Fin de l'URL ngrok "
     "éphémère et du poste local allumé en permanence. Modal ne sert plus que le GPU.",
     "Highest", "Done", ["infra", "prod"], "PROD"),
    (T, "Mettre en place une sauvegarde quotidienne de la base",
     "La base SQLite vit aujourd'hui dans un volume de conteneur, sans copie. Une "
     "réservation perdue est un client perdu. Sauvegarde quotidienne hors machine + "
     "test de restauration.",
     "Highest", "To Do", ["infra", "prod", "risque"], "PROD"),
    (T, "Supervision et alerte en cas d'appel en échec",
     "Aujourd'hui une panne se découvre en écoutant un appel. Il faut une alerte sur "
     "webhook en erreur, TTS injoignable, taux d'échec anormal.",
     "High", "To Do", ["observabilite", "prod"], "PROD"),
    (T, "Arrêter l'ancienne app Modal monolithique",
     "deploy/modal_app.py hébergeait toute l'application sur GPU. Legacy depuis la "
     "migration : `modal app stop moshi-voice-assistant` pour ne plus gaspiller de GPU.",
     "Low", "To Do", ["modal", "nettoyage"], "PROD"),

    # ---------------------------------------------------------------- QUALITE
    (S, "Intégration continue GitHub Actions sur push et pull request",
     "pytest à chaque push, aucun appel réseau dans les tests.",
     "High", "Done", ["ci", "tests"], "QUALITE"),
    (T, "Suite de tests sans réseau : 220 tests au vert",
     "LLM, STT et TTS entièrement simulés. Couvre le routage, les outils, l'admin, les "
     "voix, le STT Kyutai et le prompt système.",
     "High", "Done", ["tests"], "QUALITE"),

    # ---------------------------------------------------------------- SCALE
    (B, "Un seul appel par conteneur : le batching de moshi-server n'était jamais utilisé",
     "Sur Modal, une connexion websocket est un input de fonction. Sans "
     "@modal.concurrent, Modal n'en place qu'un par conteneur : le deuxième appel "
     "simultané allumait un deuxième GPU, cold start compris, alors que moshi-server "
     "est configuré pour partager le process (batch_size=8). Corrigé par "
     "@modal.concurrent(max_inputs=8, target_inputs=6).",
     "Highest", "In Progress", ["scale", "modal", "cout"], "SCALE"),
    (T, "Plafonner le nombre de GPU simultanés",
     "Sans plafond, un pic d'appels ou une boucle de reconnexion peut allumer des "
     "dizaines de L4. max_containers=4, soit 32 appels simultanés, réglable par "
     "variable d'environnement.",
     "High", "In Progress", ["scale", "modal", "cout"], "SCALE"),
    (T, "Redéployer Modal et valider deux appels simultanés sur un seul GPU",
     "Critère de recette : deux appels lancés en même temps depuis deux téléphones "
     "doivent apparaître comme UN SEUL conteneur dans le tableau de bord Modal.",
     "Highest", "To Do", ["scale", "modal", "recette"], "SCALE"),
    (T, "Garder un GPU chaud aux heures de service uniquement",
     "Le scale-to-zero fait payer un cold start d'environ 50 s au premier appel après "
     "2 minutes d'inactivité — soit précisément le coup de feu de 19 h. Un L4 chaud en "
     "continu coûte environ 520 €/mois, contre environ 170 €/mois sur 8 h par jour, "
     "amorti sur tous les établissements. Piloter MODAL_MIN_CONTAINERS par planning.",
     "High", "To Do", ["scale", "cout", "latence"], "SCALE"),

    # ---------------------------------------------------------------- SECU
    (T, "Faire tourner toutes les clés d'API exposées en clair",
     "Twilio (auth token), OpenRouter, Deepgram, Cartesia et Anthropic ont été "
     "affichées en clair et sont à considérer comme compromises. À révoquer et "
     "regénérer côté fournisseurs. Les secrets vivent dans .env (non versionné), "
     "jamais dans env.example.",
     "Highest", "To Do", ["securite", "risque"], "SECU"),
    (T, "Auditer la protection de l'admin exposé publiquement",
     "app.helmane.fr/admin est désormais sur Internet : vérifier l'authentification, "
     "la protection CSRF, le scoping tenant sur chaque route et la robustesse des mots "
     "de passe.",
     "Highest", "To Do", ["securite", "admin"], "SECU"),
    (T, "Obligations RGPD sur les données d'appelants",
     "Numéro de téléphone et contenu de conversation sont des données personnelles : "
     "registre de traitement, durée de conservation, mention d'information au début de "
     "l'appel, contrat de sous-traitance avec les clients, hébergement en Europe.",
     "High", "To Do", ["securite", "conformite", "juridique"], "SECU"),

    # ---------------------------------------------------------------- GTM
    (S, "Réécrire le prompt système de l'assistante",
     "Trois manques corrigés : prononciation (heures et dates en toutes lettres à "
     "l'oral, format strict conservé dans les appels d'outils) ; dates relatives "
     "(« vendredi prochain » exigeait de connaître le jour de la semaine) ; garde-fous "
     "absents sur les appels qui ne sont pas des réservations — allergènes, annulation, "
     "gestes commerciaux, hors-sujet, tentative de changement de rôle, ligne inaudible. "
     "11 tests ajoutés.",
     "High", "Done", ["llm", "produit"], "GTM"),
    (S, "Page de vente Helmane",
     "Héro montrant un appel réel qui se déroule et la ligne qui s'inscrit au cahier, "
     "tarifs présentés en carte de restaurant, calculateur de manque à gagner piloté "
     "par les chiffres du restaurateur, section « ce qu'elle ne fera jamais ». Aucun "
     "faux témoignage ni fausse statistique.",
     "High", "Done", ["marketing"], "GTM"),
    (T, "Arrêter la grille tarifaire",
     "Proposition : Essentiel 89 € (150 appels), Service 149 € (400 appels), Maison "
     "249 € (5 établissements, 1 200 appels). Marché français constaté : Yumcall 99 €, "
     "Accueil IA 39 à 149 €, Nerolia à partir de 149 €. Marché américain : Loman "
     "environ 299 $, Slang.ai à partir de 399 $. Coût de revient mesuré : 11,4 ¢ par "
     "appel, soit plus de 80 % de marge à 89 € pour 150 appels. Tarif fondateur "
     "conseillé pour les 10 premiers, prix garanti à vie.",
     "Highest", "To Do", ["marketing", "decision"], "GTM"),
    (T, "Ouvrir un numéro de démonstration public",
     "Le meilleur argument d'un service vocal est « appelez-la maintenant ». Numéro "
     "Twilio dédié, établissement fictif de démonstration, à placer sur la page de "
     "vente (l'emplacement est déjà prévu).",
     "High", "To Do", ["marketing", "twilio"], "GTM"),
    (S, "Compter les appels et faire respecter le plafond de la formule",
     "Le plafond est un argument de vente assumé et la seule protection contre "
     "l'établissement à 800 appels par mois qui mange la marge GPU. Compteur mensuel "
     "par établissement, alerte à 80 %, facturation du dépassement à 0,15 €.",
     "High", "To Do", ["produit", "facturation"], "GTM"),
    (T, "Recruter un restaurant pilote pour deux semaines de vrais appels",
     "Jalon de sortie de la phase produit. On ne saura pas ce qui casse tant qu'un vrai "
     "client n'appelle pas.",
     "Highest", "To Do", ["marketing", "pilote"], "GTM"),

    # ---------------------------------------------------------------- SAAS
    (S, "Modification et annulation de réservation par téléphone",
     "Aujourd'hui l'assistante prend le message et l'équipe rappelle — et il lui est "
     "interdit de prétendre avoir annulé. C'est la première capacité manquante citée "
     "par les concurrents.",
     "High", "To Do", ["produit", "llm"], "SAAS"),
    (S, "SMS de confirmation au client et récapitulatif au restaurateur",
     "Confirmation immédiate au client final, récapitulatif quotidien à "
     "l'établissement. Argument de vente du palier Service.",
     "Medium", "To Do", ["produit", "twilio"], "SAAS"),
    (S, "Transfert d'appel vers un humain",
     "Sur demande explicite, mot-clé d'urgence, ou après deux incompréhensions "
     "consécutives.",
     "Medium", "To Do", ["produit", "twilio"], "SAAS"),
    (S, "Facturation Stripe : abonnement et dépassement",
     "Abonnement mensuel par formule, dépassement à l'appel, portail client "
     "d'auto-gestion.",
     "High", "To Do", ["facturation"], "SAAS"),
    (S, "Achat du numéro Twilio en un clic depuis l'admin",
     "Objectif d'onboarding : moins de 15 minutes sans intervention manuelle.",
     "Medium", "To Do", ["produit", "twilio"], "SAAS"),
    (T, "Passer de SQLite à PostgreSQL",
     "À faire quand plusieurs établissements écrivent en même temps, pas avant : "
     "SQLite tient largement la charge actuelle.",
     "Low", "To Do", ["backend", "bdd"], "SAAS"),
]

MAX_LABELS = 3
HEADER = (["Issue Type", "Summary", "Description", "Priority", "Status", "Epic Name",
           "Epic Link"] + ["Labels"] * MAX_LABELS)

out = pathlib.Path(__file__).resolve().parents[1] / "docs" / "backlog-jira.csv"

with out.open("w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
    w.writerow(HEADER)

    for key, nom, desc in EPICS:
        w.writerow([E, nom, desc, "High", "To Do", nom, ""] + [""] * MAX_LABELS)

    epic_nom = {k: n for k, n, _ in EPICS}
    for typ, resume, desc, prio, statut, labels, epic in ROWS:
        labs = (list(labels) + [""] * MAX_LABELS)[:MAX_LABELS]
        w.writerow([typ, resume, desc, prio, statut, "", epic_nom[epic]] + labs)

total = len(EPICS) + len(ROWS)
faits = sum(1 for r in ROWS if r[4] == "Done")
cours = sum(1 for r in ROWS if r[4] == "In Progress")
todo = sum(1 for r in ROWS if r[4] == "To Do")
print(f"{out}")
print(f"{len(EPICS)} epics + {len(ROWS)} tickets = {total} lignes")
print(f"Done {faits} · In Progress {cours} · To Do {todo}")
